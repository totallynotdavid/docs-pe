from __future__ import annotations

import asyncio
import contextlib
import json

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import httpx


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from asyncpg import Pool

    from portal.notify.mailer import Mailer


DISPATCH_INTERVAL_SECONDS = 15.0
RETRY_BACKOFF_SECONDS = 300.0
CLAIM_BATCH_SIZE = 20

_ESTADO_LABELS = {
    "completed": "completada",
    "failed": "con error",
    "cancelled": "cancelada",
}

# Claim rows before the network call so the transaction does not hold a lock
# while the mail provider responds.
_CLAIM_PENDING_EMAIL = """
WITH claimed AS (
    SELECT id
      FROM portal_notification_outbox
     WHERE state = 'pending'
       AND channel = 'email'
       AND available_at <= now()
     ORDER BY available_at, id
     FOR UPDATE SKIP LOCKED
     LIMIT $1
)
UPDATE portal_notification_outbox AS outbox
   SET state = 'sending'
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
        # The job's submitter no longer resolves to a live account. Nothing
        # to retry: mark it sent so the row stops being claimed.
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
    async with pool.acquire() as connection, connection.transaction():
        claimed = await connection.fetch(_CLAIM_PENDING_EMAIL, CLAIM_BATCH_SIZE)

    for row in claimed:
        await _send_one(pool, mailer, row["id"], row["event_id"], row["payload"])


@contextlib.asynccontextmanager
async def dispatching(pool: Pool, mailer: Mailer) -> AsyncIterator[None]:
    """Run the email outbox dispatcher for as long as the app is up."""

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
