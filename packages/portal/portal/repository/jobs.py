from __future__ import annotations

import json

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from core.sites.registry import STABLE_SITES

from portal.domain.errors import Reason, SourceValidationError
from portal.domain.models import (
    MAX_ACTIVE_JOBS,
    MAX_LEASE_ATTEMPTS,
    AttemptRecord,
    ClaimedWork,
    ExcludedInput,
    ItemState,
    Job,
    JobCredential,
    JobEvent,
    JobItem,
    JobItemCounts,
    JobNotification,
    JobState,
    ProtectedSecret,
    QueueHealth,
    SubmissionPlan,
    SubmitJob,
)
from portal.storage.port import ObjectReference


if TYPE_CHECKING:
    from collections.abc import Sequence

    from asyncpg import Connection, Pool


_LOCK_QUEUE_GATE = """
SELECT max_active_jobs
  FROM portal_queue_control
 WHERE singleton = true
 FOR UPDATE
"""

_ACTIVE_COUNT = """
SELECT count(*)
  FROM portal_jobs
 WHERE state IN ('running', 'cancelling')
"""

_PROMOTE_FIFO = """
WITH next_jobs AS (
    SELECT id
      FROM portal_jobs
     WHERE state = 'queued'
     ORDER BY queue_sequence
     FOR UPDATE SKIP LOCKED
     LIMIT $1
)
UPDATE portal_jobs job
   SET state = 'running', started_at = now()
 WHERE job.id IN (SELECT id FROM next_jobs)
RETURNING job.id
"""

_CLAIM_ONE = """
WITH candidate AS (
    SELECT item.id, job.lease_fence, job.credential_version_id
      FROM portal_job_items AS item
      JOIN portal_jobs AS job ON job.id = item.job_id
      JOIN portal_team_proxy_credential_versions AS version
        ON version.id = job.credential_version_id
      LEFT JOIN portal_circuit_breakers AS breaker
        ON breaker.source = item.source
       AND breaker.provider = version.provider
     WHERE item.state = 'pending'
       AND job.state = 'running'
       AND item.source = ANY($2::text[])
       AND (breaker.open_until IS NULL OR breaker.open_until <= now())
     ORDER BY
       -- Prefer work matching the lane's current provider session.
       CASE
           WHEN item.source = $3 AND job.credential_version_id = $4 THEN 0
           ELSE 1
       END,
       job.queue_sequence,
       item.ordinal
     FOR UPDATE OF item SKIP LOCKED
     LIMIT 1
)
UPDATE portal_job_items AS item
   SET state = 'running',
       lease_owner = $1,
       lease_fence = candidate.lease_fence,
       lease_expires_at = now() + interval '5 minutes',
       attempts = item.attempts + 1
  FROM candidate
 WHERE item.id = candidate.id
RETURNING
    item.id,
    item.job_id,
    item.source,
    item.document,
    item.lease_fence,
    candidate.credential_version_id
"""

# Only running jobs may recover expired items. Cancellation makes its items
# terminal and advances the job fence before a late worker can publish.
_SWEEP_EXPIRED = """
WITH expired AS (
    SELECT item.id, item.attempts
      FROM portal_job_items AS item
      JOIN portal_jobs AS job ON job.id = item.job_id
     WHERE item.state = 'running'
       AND item.lease_expires_at < now()
       AND job.state = 'running'
     FOR UPDATE OF item SKIP LOCKED
)
UPDATE portal_job_items AS item
   SET state = CASE
           WHEN expired.attempts >= $1 THEN 'failed'
           ELSE 'pending'
       END,
       reason = CASE
           WHEN expired.attempts >= $1 THEN 'lease_expired'
           ELSE item.reason
       END,
       finished_at = CASE
           WHEN expired.attempts >= $1 THEN now()
           ELSE NULL
       END,
       lease_owner = NULL,
       lease_expires_at = NULL
  FROM expired
 WHERE item.id = expired.id
RETURNING item.job_id
"""

_PUBLISH_FENCED = """
UPDATE portal_job_items AS item
   SET state = 'published',
       entry_id = $4,
       result_object_id = $5,
       published_at = now(),
       finished_at = now(),
       lease_owner = NULL,
       lease_expires_at = NULL
  FROM portal_jobs AS job
 WHERE item.id = $1
   AND item.job_id = job.id
   AND item.state = 'running'
   AND item.lease_owner = $2
   AND item.lease_fence = $3
   AND job.state = 'running'
   AND job.lease_fence = $3
RETURNING item.job_id
"""

# unnest(...) AS attempt(...) zips the four parallel arrays row-wise, the
# same multi-column unnest idiom PostgresProxySlots.renew() uses (see
# repository/slots.py) to write several rows in one round trip.
_INSERT_ATTEMPTS = """
INSERT INTO portal_lookup_attempts
    (id, job_item_id, source, provider, worker_id, lane_index,
     fetch_attempt, outcome, error_code, elapsed_ms)
SELECT
    attempt.id, $1, $2, $3, $4, $5,
    attempt.fetch_attempt, attempt.outcome, attempt.error_code, attempt.elapsed_ms
  FROM unnest($6::uuid[], $7::int[], $8::text[], $9::text[], $10::int[])
    AS attempt(id, fetch_attempt, outcome, error_code, elapsed_ms)
"""

_UPSERT_ENTRY = """
INSERT INTO portal_entries (
    id, document, source, status, columns, rows, error_code, last_job_id
)
VALUES (
    $1, $2, $3, $4, $5, $6::jsonb, $7,
    (SELECT job_id FROM portal_job_items WHERE id = $8)
)
ON CONFLICT (document, source) DO UPDATE
    SET status = EXCLUDED.status,
        columns = EXCLUDED.columns,
        rows = EXCLUDED.rows,
        error_code = EXCLUDED.error_code,
        last_confirmed_at = now(),
        last_job_id = EXCLUDED.last_job_id
RETURNING id
"""


class PostgresJobRepository:
    """PostgreSQL job lifecycle and worker queue."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def admit_submission(
        self,
        command: SubmitJob,
        plan: SubmissionPlan,
        reusable: dict[tuple[str, str], UUID],
    ) -> Job:
        """Create a job with reusable items already published."""
        job_id = uuid4()
        to_fetch = [
            item for item in plan.items if (item.document, item.source) not in reusable
        ]
        reused = [
            item for item in plan.items if (item.document, item.source) in reusable
        ]

        async with self._pool.acquire() as connection, connection.transaction():
            max_active = await self._lock_queue_gate(connection)
            active = await connection.fetchval(_ACTIVE_COUNT)

            # A job with nothing left to fetch is done the instant it's
            # created, whether that's because every line was excluded or
            # because this team already had a fresh answer for all of it.
            state = (
                JobState.COMPLETED
                if not to_fetch
                else JobState.RUNNING
                if int(active) < max_active
                else JobState.QUEUED
            )
            terminal_reason = "all_records_excluded" if not plan.items else None

            row = await connection.fetchrow(
                """
                INSERT INTO portal_jobs (
                    id,
                    team_id,
                    submitted_by,
                    credential_version_id,
                    input_object_id,
                    filename,
                    sources,
                    state,
                    terminal_reason
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                RETURNING queue_sequence
                """,
                job_id,
                command.team_id,
                command.actor_id,
                command.credential_version_id,
                command.input_object_id,
                command.filename,
                list(command.sources),
                state.value,
                terminal_reason,
            )

            await connection.executemany(
                """
                INSERT INTO portal_job_items (
                    id,
                    job_id,
                    team_id,
                    ordinal,
                    document,
                    source,
                    state
                )
                VALUES ($1, $2, $3, $4, $5, $6, 'pending')
                """,
                [
                    (
                        uuid4(),
                        job_id,
                        command.team_id,
                        item.ordinal,
                        item.document,
                        item.source,
                    )
                    for item in to_fetch
                ],
            )

            await connection.executemany(
                """
                INSERT INTO portal_job_items (
                    id,
                    job_id,
                    team_id,
                    ordinal,
                    document,
                    source,
                    state,
                    entry_id,
                    published_at,
                    finished_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, 'published', $7, now(), now())
                """,
                [
                    (
                        uuid4(),
                        job_id,
                        command.team_id,
                        item.ordinal,
                        item.document,
                        item.source,
                        reusable[item.document, item.source],
                    )
                    for item in reused
                ],
            )

            await connection.executemany(
                """
                INSERT INTO portal_job_items (
                    id,
                    job_id,
                    team_id,
                    ordinal,
                    document,
                    source,
                    state,
                    reason
                )
                VALUES ($1, $2, $3, $4, $5, NULL, 'excluded', $6)
                """,
                [
                    (
                        uuid4(),
                        job_id,
                        command.team_id,
                        item.ordinal,
                        item.value,
                        item.reason,
                    )
                    for item in plan.exclusions
                ],
            )

            if state is JobState.COMPLETED:
                await self._terminal_intents(
                    connection,
                    job_id,
                    JobState.COMPLETED,
                )
            else:
                await self._event(
                    connection,
                    job_id,
                    f"job.{state.value}",
                    command.actor_id,
                )

        return Job(
            id=job_id,
            team_id=command.team_id,
            submitted_by=command.actor_id,
            credential_version_id=command.credential_version_id,
            input_object_id=command.input_object_id,
            filename=command.filename,
            sources=command.sources,
            queue_sequence=int(row["queue_sequence"]),
            state=state,
        )

    async def cancel(self, job_id: UUID, team_id: UUID) -> Job | None:
        async with self._pool.acquire() as connection, connection.transaction():
            await self._lock_queue_gate(connection)

            row = await connection.fetchrow(
                """
                SELECT *
                  FROM portal_jobs
                 WHERE id = $1
                   AND team_id = $2
                 FOR UPDATE
                """,
                job_id,
                team_id,
            )

            if row is None:
                return None

            state = JobState(row["state"])

            if state not in {
                JobState.QUEUED,
                JobState.RUNNING,
                JobState.CANCELLING,
            }:
                return self._job(row)

            if state is JobState.RUNNING:
                await connection.execute(
                    """
                    UPDATE portal_jobs
                       SET state = 'cancelling',
                           lease_fence = lease_fence + 1
                     WHERE id = $1
                    """,
                    job_id,
                )

                await self._event(
                    connection,
                    job_id,
                    "job.cancellation_requested",
                    None,
                )

            # Published objects remain valid. The advanced fence rejects late writes.
            await connection.execute(
                """
                UPDATE portal_job_items
                   SET state = 'cancelled',
                       finished_at = now()
                 WHERE job_id = $1
                   AND state IN ('pending', 'running')
                """,
                job_id,
            )

            row = await connection.fetchrow(
                """
                UPDATE portal_jobs
                   SET state = 'cancelled',
                       finished_at = now()
                 WHERE id = $1
                RETURNING *
                """,
                job_id,
            )

            await self._terminal_intents(
                connection,
                job_id,
                JobState.CANCELLED,
            )
            await self._promote_locked(connection)

        return self._job(row)

    async def recent_job_events(
        self,
        team_ids: tuple[UUID, ...],
        event_types: tuple[str, ...],
        *,
        limit: int,
    ) -> tuple[JobNotification, ...]:
        if not team_ids or not event_types:
            return ()

        rows = await self._pool.fetch(
            """
            SELECT
                event.id,
                event.job_id,
                event.event_type,
                event.created_at,
                job.team_id,
                job.filename,
                team.name AS team_name
              FROM portal_job_events AS event
              JOIN portal_jobs AS job ON job.id = event.job_id
              JOIN portal_teams AS team ON team.id = job.team_id
             WHERE job.team_id = ANY($1::uuid[])
               AND event.event_type = ANY($2::text[])
             ORDER BY event.sequence DESC
             LIMIT $3
            """,
            list(team_ids),
            list(event_types),
            limit,
        )

        return tuple(
            JobNotification(
                id=row["id"],
                job_id=row["job_id"],
                team_id=row["team_id"],
                team_name=row["team_name"],
                filename=row["filename"],
                event_type=row["event_type"],
                created_at=row["created_at"],
            )
            for row in rows
        )

    async def published_jobs(self, team_id: UUID) -> tuple[Job, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT DISTINCT job.*
                  FROM portal_jobs AS job
                  JOIN portal_job_items AS item ON item.job_id = job.id
                 WHERE job.team_id = $1
                   AND item.state = 'published'
                 ORDER BY job.queue_sequence
                """,
                team_id,
            )

        return tuple(self._job(row) for row in rows)

    async def queue_health(self) -> QueueHealth:
        async with self._pool.acquire() as connection:
            active = int(await connection.fetchval(_ACTIVE_COUNT))
            queued = int(
                await connection.fetchval(
                    "SELECT count(*) FROM portal_jobs WHERE state = 'queued'"
                )
            )

        return QueueHealth(
            active_jobs=active,
            max_active_jobs=MAX_ACTIVE_JOBS,
            queued_jobs=queued,
        )

    async def add_object_reference(self, reference: ObjectReference) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO portal_object_references (
                    id,
                    team_id,
                    provider,
                    container,
                    object_key,
                    sha256,
                    size_bytes,
                    content_type
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                reference.id,
                reference.team_id,
                reference.provider,
                reference.container,
                reference.object_key,
                reference.sha256,
                reference.size_bytes,
                reference.content_type,
            )

    async def jobs_for_team(
        self,
        team_id: UUID,
        *,
        page: int,
        page_size: int,
    ) -> tuple[tuple[Job, ...], int]:
        offset = (page - 1) * page_size

        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT *
                  FROM portal_jobs
                 WHERE team_id = $1
                 ORDER BY queue_sequence DESC
                 LIMIT $2
                OFFSET $3
                """,
                team_id,
                page_size,
                offset,
            )

            total = await connection.fetchval(
                "SELECT count(*) FROM portal_jobs WHERE team_id = $1",
                team_id,
            )

        return tuple(self._job(row) for row in rows), int(total)

    async def job(self, job_id: UUID, team_id: UUID) -> Job | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT *
                  FROM portal_jobs
                 WHERE id = $1
                   AND team_id = $2
                """,
                job_id,
                team_id,
            )

            if row is None:
                return None

            item_rows = await connection.fetch(
                """
                SELECT
                    id,
                    ordinal,
                    document,
                    source,
                    state,
                    lease_fence,
                    entry_id,
                    result_object_id,
                    reason
                  FROM portal_job_items
                 WHERE job_id = $1
                 ORDER BY ordinal, source
                """,
                job_id,
            )

        job = self._job(row)

        for item in item_rows:
            if item["state"] == "excluded":
                job.exclusions.append(
                    ExcludedInput(
                        int(item["ordinal"]),
                        item["document"],
                        item["reason"],
                    )
                )
                continue

            job.items.append(self._job_item(item))

        return job

    async def items_for_job(
        self,
        job_id: UUID,
        team_id: UUID,
        *,
        page: int,
        page_size: int,
    ) -> tuple[tuple[JobItem, ...], int]:
        """Return a page of non-excluded items and the total count."""
        offset = (page - 1) * page_size

        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT id, ordinal, document, source, state, lease_fence,
                       entry_id, result_object_id
                  FROM portal_job_items
                 WHERE job_id = $1
                   AND team_id = $2
                   AND state != 'excluded'
                 ORDER BY ordinal, source
                 LIMIT $3
                OFFSET $4
                """,
                job_id,
                team_id,
                page_size,
                offset,
            )

            total = await connection.fetchval(
                """
                SELECT count(*)
                  FROM portal_job_items
                 WHERE job_id = $1
                   AND team_id = $2
                   AND state != 'excluded'
                """,
                job_id,
                team_id,
            )

        items = tuple(self._job_item(row) for row in rows)

        return items, int(total)

    async def all_items_for_job(
        self,
        job_id: UUID,
        team_id: UUID,
    ) -> tuple[JobItem, ...]:
        """Every non-excluded item for a job, unpaginated. Used for the full
        results export, where a leader needs every row, not one page."""
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT id, ordinal, document, source, state, lease_fence,
                       entry_id, result_object_id
                  FROM portal_job_items
                 WHERE job_id = $1
                   AND team_id = $2
                   AND state != 'excluded'
                 ORDER BY ordinal, source
                """,
                job_id,
                team_id,
            )

        return tuple(self._job_item(row) for row in rows)

    async def item_counts(self, job_id: UUID, team_id: UUID) -> JobItemCounts:
        """Count non-excluded item states without loading item details."""

        rows = await self._pool.fetch(
            """
            SELECT item.state, count(*) AS count
              FROM portal_job_items AS item
              JOIN portal_jobs AS job ON job.id = item.job_id
             WHERE item.job_id = $1
               AND job.team_id = $2
               AND item.state != 'excluded'
             GROUP BY item.state
            """,
            job_id,
            team_id,
        )
        by_state = {row["state"]: int(row["count"]) for row in rows}

        return JobItemCounts(
            pending=by_state.get(ItemState.PENDING.value, 0),
            running=by_state.get(ItemState.RUNNING.value, 0),
            published=by_state.get(ItemState.PUBLISHED.value, 0),
            failed=by_state.get(ItemState.FAILED.value, 0),
            cancelled=by_state.get(ItemState.CANCELLED.value, 0),
        )

    async def job_events_after(
        self,
        job_id: UUID,
        team_id: UUID,
        sequence: int,
    ) -> tuple[JobEvent, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT
                    event.id,
                    event.job_id,
                    event.event_type,
                    event.sequence,
                    event.created_at
                  FROM portal_job_events AS event
                  JOIN portal_jobs AS job ON job.id = event.job_id
                 WHERE event.job_id = $1
                   AND job.team_id = $2
                   AND event.sequence > $3
                 ORDER BY event.sequence
                """,
                job_id,
                team_id,
                sequence,
            )

        return tuple(
            JobEvent(
                row["id"],
                row["job_id"],
                row["event_type"],
                int(row["sequence"]),
                row["created_at"],
            )
            for row in rows
        )

    async def claim(
        self,
        worker_id: str,
        sources: tuple[str, ...],
        *,
        affinity_source: str | None = None,
        affinity_credential_version_id: UUID | None = None,
    ) -> ClaimedWork | None:
        if not sources:
            raise SourceValidationError(Reason.WORKER_SOURCE_REQUIRED)

        invalid = sorted(set(sources).difference(STABLE_SITES))

        if invalid:
            raise SourceValidationError(
                Reason.SOURCE_NOT_ENABLED,
                invalid=", ".join(invalid),
                allowed=", ".join(sorted(STABLE_SITES)),
            )

        async with self._pool.acquire() as connection, connection.transaction():
            await self._sweep_expired_locked(connection)

            row = await connection.fetchrow(
                _CLAIM_ONE,
                worker_id,
                list(sources),
                affinity_source,
                affinity_credential_version_id,
            )

        if row is None:
            return None

        return ClaimedWork(
            item_id=row["id"],
            job_id=row["job_id"],
            source=row["source"],
            document=row["document"],
            lease_fence=int(row["lease_fence"]),
            credential_version_id=row["credential_version_id"],
        )

    async def publish(
        self,
        item_id: UUID,
        worker_id: str,
        fence: int,
        *,
        document: str,
        source: str,
        provider: str,
        status: str,
        columns: tuple[str, ...],
        rows: tuple[tuple[object, ...], ...],
        error_code: str | None,
        result_object_id: UUID,
        lane_index: int,
        attempts: Sequence[AttemptRecord],
    ) -> bool:
        """Publish only while the worker still owns both fences.

        Upserts portal_entries and records every fetch_one attempt first:
        both are a correct, useful record of what actually happened against
        the site regardless of whether this particular item's fence still
        holds, so neither belongs behind the fencing check that follows.
        """

        async with self._pool.acquire() as connection, connection.transaction():
            entry_id = await connection.fetchval(
                _UPSERT_ENTRY,
                uuid4(),
                document,
                source,
                status,
                list(columns),
                json.dumps(rows),
                error_code,
                item_id,
            )

            if attempts:
                await connection.execute(
                    _INSERT_ATTEMPTS,
                    item_id,
                    source,
                    provider,
                    worker_id,
                    lane_index,
                    [uuid4() for _ in attempts],
                    [a.fetch_attempt for a in attempts],
                    [a.outcome for a in attempts],
                    [a.error_code for a in attempts],
                    [a.elapsed_ms for a in attempts],
                )

            job_id = await connection.fetchval(
                _PUBLISH_FENCED,
                item_id,
                worker_id,
                fence,
                entry_id,
                result_object_id,
            )

            if job_id is None:
                return False

            await self._finish_if_drained_locked(connection, job_id)

        return True

    async def credential_for_job(
        self,
        job_id: UUID,
    ) -> JobCredential | None:
        row = await self._pool.fetchrow(
            """
            SELECT
                version.provider,
                version.config_ciphertext,
                version.wrapped_data_key,
                version.master_key_version
              FROM portal_jobs AS job
              JOIN portal_team_proxy_credential_versions AS version
                ON version.id = job.credential_version_id
             WHERE job.id = $1
               AND version.wrapped_data_key IS NOT NULL
            """,
            job_id,
        )

        if row is None:
            return None

        return JobCredential(
            row["provider"],
            ProtectedSecret(
                ciphertext=bytes(row["config_ciphertext"]),
                wrapped_data_key=bytes(row["wrapped_data_key"]),
                master_key_version=str(row["master_key_version"]),
            ),
        )

    async def object_reference(
        self,
        reference_id: UUID,
        team_id: UUID,
    ) -> ObjectReference | None:
        row = await self._pool.fetchrow(
            """
            SELECT id, team_id, provider, container, object_key,
                   sha256, size_bytes, content_type
              FROM portal_object_references
             WHERE id = $1
               AND team_id = $2
            """,
            reference_id,
            team_id,
        )

        if row is None:
            return None

        return ObjectReference(
            id=row["id"],
            team_id=row["team_id"],
            provider=row["provider"],
            container=row["container"],
            object_key=row["object_key"],
            sha256=row["sha256"],
            size_bytes=int(row["size_bytes"]),
            content_type=row["content_type"],
        )

    async def item_team(self, item_id: UUID) -> UUID | None:
        team_id: UUID | None = await self._pool.fetchval(
            "SELECT team_id FROM portal_job_items WHERE id = $1",
            item_id,
        )

        return team_id

    async def _lock_queue_gate(self, connection: Connection) -> int:
        maximum = await connection.fetchval(_LOCK_QUEUE_GATE)

        if maximum != MAX_ACTIVE_JOBS:
            raise RuntimeError(
                "portal_queue_control must retain the fixed five-process limit"
            )

        return int(maximum)

    async def _promote_locked(self, connection: Connection) -> None:
        active = int(await connection.fetchval(_ACTIVE_COUNT))
        slots = MAX_ACTIVE_JOBS - active

        if slots <= 0:
            return

        promoted = await connection.fetch(_PROMOTE_FIFO, slots)

        for row in promoted:
            await self._event(
                connection,
                row["id"],
                "job.running",
                None,
            )

    async def _sweep_expired_locked(self, connection: Connection) -> None:
        """Recover expired leases. A drain takes the queue gate itself."""

        rows = await connection.fetch(
            _SWEEP_EXPIRED,
            MAX_LEASE_ATTEMPTS,
        )

        for job_id in {row["job_id"] for row in rows}:
            await self._finish_if_drained_locked(connection, job_id)

    async def _finish_if_drained_locked(
        self,
        connection: Connection,
        job_id: UUID,
    ) -> None:
        # A drained job with no published item failed; it was not an empty success.
        row = await connection.fetchrow(
            """
            UPDATE portal_jobs AS job
               SET state = CASE
                       WHEN EXISTS (
                           SELECT 1
                             FROM portal_job_items
                            WHERE job_id = $1
                              AND state = 'published'
                       )
                       THEN 'completed'
                       ELSE 'failed'
                   END,
                   terminal_reason = CASE
                       WHEN EXISTS (
                           SELECT 1
                             FROM portal_job_items
                            WHERE job_id = $1
                              AND state = 'published'
                       )
                       THEN job.terminal_reason
                       ELSE 'no_results'
                   END,
                   finished_at = now()
             WHERE job.id = $1
               AND job.state = 'running'
               AND NOT EXISTS (
                   SELECT 1
                     FROM portal_job_items
                    WHERE job_id = $1
                      AND state IN ('pending', 'running')
               )
            RETURNING job.state
            """,
            job_id,
        )

        if row is None:
            return

        await self._terminal_intents(
            connection,
            job_id,
            JobState(row["state"]),
        )
        # Promotion reads and updates the fleet-wide active-job count, so it
        # needs the queue gate. Only a drain reaches this point, so claim()
        # and publish() no longer serialize on every call just to get here.
        await self._lock_queue_gate(connection)
        await self._promote_locked(connection)

    async def _event(
        self,
        connection: Connection,
        job_id: UUID,
        event_type: str,
        actor_id: UUID | None,
    ) -> UUID:
        event_id = uuid4()

        await connection.execute(
            """
            INSERT INTO portal_job_events (
                id,
                job_id,
                event_type,
                actor_id
            )
            VALUES ($1, $2, $3, $4)
            """,
            event_id,
            job_id,
            event_type,
            actor_id,
        )

        return event_id

    async def _terminal_intents(
        self,
        connection: Connection,
        job_id: UUID,
        state: JobState,
    ) -> None:
        event_id = await self._event(
            connection,
            job_id,
            f"job.{state.value}",
            None,
        )

        await connection.executemany(
            """
            INSERT INTO portal_notification_outbox (
                id,
                event_id,
                channel,
                payload
            )
            VALUES (
                $1,
                $2,
                $3,
                jsonb_build_object(
                    'job_id',
                    $4::text,
                    'estado',
                    $5::text
                )
            )
            """,
            [
                (
                    uuid4(),
                    event_id,
                    channel,
                    str(job_id),
                    state.value,
                )
                for channel in ("in_app", "email", "kapso_whatsapp")
            ],
        )

    @staticmethod
    def _job(row: object) -> Job:
        return Job(
            id=row["id"],  # type: ignore[index]
            team_id=row["team_id"],  # type: ignore[index]
            submitted_by=row["submitted_by"],  # type: ignore[index]
            credential_version_id=row["credential_version_id"],  # type: ignore[index]
            input_object_id=row["input_object_id"],  # type: ignore[index]
            filename=row["filename"],  # type: ignore[index]
            sources=tuple(row["sources"]),  # type: ignore[index]
            queue_sequence=int(row["queue_sequence"]),  # type: ignore[index]
            state=JobState(row["state"]),  # type: ignore[index]
            lease_fence=int(row["lease_fence"]),  # type: ignore[index]
            terminal_reason=row["terminal_reason"],  # type: ignore[index]
            created_at=row["created_at"],  # type: ignore[index]
        )

    @staticmethod
    def _job_item(row: object) -> JobItem:
        return JobItem(
            id=row["id"],  # type: ignore[index]
            ordinal=int(row["ordinal"]),  # type: ignore[index]
            document=row["document"],  # type: ignore[index]
            source=row["source"],  # type: ignore[index]
            state=ItemState(row["state"]),  # type: ignore[index]
            lease_fence=int(row["lease_fence"]),  # type: ignore[index]
            entry_id=row["entry_id"],  # type: ignore[index]
            result_object_id=row["result_object_id"],  # type: ignore[index]
        )
