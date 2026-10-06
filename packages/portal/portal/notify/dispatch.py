from __future__ import annotations

import asyncio
import contextlib
import json

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import httpx

from portal.notify.mailer import SEND_TIMEOUT_SECONDS


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from asyncpg import Pool

    from portal.notify.mailer import Mailer


DISPATCH_INTERVAL_SECONDS = 15.0
RETRY_BACKOFF_SECONDS = 300.0
MAX_SENDS_PER_RUN = 20

# httpx applies SEND_TIMEOUT_SECONDS to connect, write, read and pool
# acquisition separately, so one send can run four times as long. The rest is
# margin for the recipient lookup and the outcome write. A lease shorter than
# a live send would let a second dispatcher send the same row.
CLAIM_LEASE_SECONDS = 6 * SEND_TIMEOUT_SECONDS

_ESTADO_LABELS = {
    "completed": "completada",
    "failed": "con error",
    "cancelled": "cancelada",
}

# Claim a row before the network call so the transaction does not hold a lock
# while the mail provider responds. The claim is a lease: available_at moves to
# its expiry, and a 'sending' row whose lease has run out belongs to a
# dispatcher that died, so it is claimable again. One row per claim keeps the
# lease covering one send, not the wait behind the rest of a batch.
_CLAIM_DUE_EMAIL = """
WITH claimed AS (
    SELECT id
      FROM portal_notification_outbox
     WHERE state IN ('pending', 'sending')
       AND channel = 'email'
       AND available_at <= now()
     ORDER BY available_at, id
     FOR UPDATE SKIP LOCKED
     LIMIT 1
)
UPDATE portal_notification_outbox AS outbox
   SET state = 'sending',
       available_at = now() + make_interval(secs => $1)
  FROM claimed
 WHERE outbox.id = claimed.id
RETURNING outbox.id, outbox.event_id, outbox.payload
"""

_RECIPIENT_FOR_EVENT = """
SELECT user_account.email
  FROM portal_job_events AS event
  JOIN portal_jobs AS job ON job.id = event.job_id
  JOIN portal_users AS user_account ON user_account.id = job.submitted_by
 WHERE event.id = $1
"""


def _render(payload: dict[str, str]) -> tuple[str, str]:
    job_id = payload.get("job_id", "")
    estado = _ESTADO_LABELS.get(payload.get("estado", ""), payload.get("estado", ""))

    return (
        f"Tarea {job_id[:8]}: {estado}",
        f"Tu tarea {job_id} terminó: {estado}.",
    )


async def _record_outcome(
    pool: Pool,
    outbox_id: UUID,
    *,
    state: str,
    outcome: str,
    detail: str,
    delay: float,
) -> None:
    async with pool.acquire() as connection, connection.transaction():
        attempt = await connection.fetchval(
            "SELECT count(*) FROM portal_notification_deliveries WHERE outbox_id = $1",
            outbox_id,
        )

        await connection.execute(
            """
            INSERT INTO portal_notification_deliveries (id, outbox_id, attempt, outcome, detail)
            VALUES ($1, $2, $3, $4, $5)
            """,
            uuid4(),
            outbox_id,
            int(attempt) + 1,
            outcome,
            detail,
        )

        await connection.execute(
            """
            UPDATE portal_notification_outbox
               SET state = $2,
                   available_at = now() + make_interval(secs => $3)
             WHERE id = $1
            """,
            outbox_id,
            state,
            delay,
        )


async def _send_one(
    pool: Pool,
    mailer: Mailer,
    outbox_id: UUID,
    event_id: UUID,
    payload_json: str,
) -> None:
    recipient = await pool.fetchval(_RECIPIENT_FOR_EVENT, event_id)

    if recipient is None:
        # The job's submitter no longer resolves to an account, so a retry
        # cannot succeed. Marking the row sent stops it being claimed.
        await _record_outcome(
            pool,
            outbox_id,
            state="sent",
            outcome="sent",
            detail="sin destinatario",
            delay=0.0,
        )
        return

    subject, body = _render(json.loads(payload_json))

    try:
        await mailer.send(to=recipient, subject=subject, body=body)
    except httpx.HTTPError as error:
        await _record_outcome(
            pool,
            outbox_id,
            state="pending",
            outcome="failed",
            detail=str(error)[:500],
            delay=RETRY_BACKOFF_SECONDS,
        )
        return

    await _record_outcome(
        pool, outbox_id, state="sent", outcome="sent", detail="", delay=0.0
    )


async def dispatch_once(pool: Pool, mailer: Mailer) -> None:
    for _ in range(MAX_SENDS_PER_RUN):
        row = await pool.fetchrow(_CLAIM_DUE_EMAIL, CLAIM_LEASE_SECONDS)

        if row is None:
            return

        await _send_one(pool, mailer, row["id"], row["event_id"], row["payload"])


@contextlib.asynccontextmanager
async def dispatching(pool: Pool, mailer: Mailer) -> AsyncGenerator[None, None]:
    async def loop() -> None:
        while True:
            await asyncio.sleep(DISPATCH_INTERVAL_SECONDS)
            await dispatch_once(pool, mailer)

    task = asyncio.create_task(loop())

    try:
        yield
    finally:
        task.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await task
