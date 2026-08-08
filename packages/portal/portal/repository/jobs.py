from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from fetch.sites.registry import STABLE_SITES

from portal.domain.errors import Reason, SourceValidationError
from portal.domain.models import (
    MAX_ACTIVE_JOBS,
    MAX_LEASE_ATTEMPTS,
    ClaimedWork,
    ExcludedInput,
    ItemState,
    Job,
    JobCredential,
    JobEvent,
    JobItem,
    JobState,
    ProtectedSecret,
    SearchResult,
    SubmissionPlan,
    SubmitJob,
)
from portal.storage.port import ObjectReference


if TYPE_CHECKING:
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
    SELECT item.id, job.lease_fence
      FROM portal_job_items AS item
      JOIN portal_jobs AS job ON job.id = item.job_id
     WHERE item.state = 'pending'
       AND job.state = 'running'
       AND item.source = ANY($2::text[])
     ORDER BY job.queue_sequence, item.ordinal
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
RETURNING item.id, item.job_id, item.source, item.document, item.lease_fence
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
       result_object_id = $4,
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


class PostgresJobRepository:
    """PostgreSQL job lifecycle and worker queue."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def admit_submission(
        self,
        command: SubmitJob,
        plan: SubmissionPlan,
    ) -> Job:
        job_id = uuid4()

        async with self._pool.acquire() as connection, connection.transaction():
            max_active = await self._lock_queue_gate(connection)
            active = await connection.fetchval(_ACTIVE_COUNT)

            state = (
                JobState.COMPLETED
                if not plan.items
                else JobState.RUNNING
                if int(active) < max_active
                else JobState.QUEUED
            )

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
                VALUES (
                    $1,
                    $2,
                    $3,
                    $4,
                    $5,
                    $6,
                    $7,
                    $8,
                    CASE
                        WHEN $8 = 'completed'
                        THEN 'all_records_excluded'
                    END
                )
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
                    for item in plan.items
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

    async def search_published(
        self,
        team_id: UUID,
        needle: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[tuple[SearchResult, ...], bool]:
        rows = await self._pool.fetch(
            """
            SELECT item.job_id, job.filename, item.document
              FROM portal_job_items AS item
              JOIN portal_jobs AS job ON job.id = item.job_id
             WHERE item.team_id = $1
               AND item.state = 'published'
               AND item.document ILIKE '%' || $2 || '%'
             ORDER BY job.queue_sequence DESC, item.ordinal
             LIMIT $3
            OFFSET $4
            """,
            team_id,
            needle,
            limit + 1,
            offset,
        )

        results = tuple(
            SearchResult(
                row["job_id"],
                row["filename"],
                row["document"],
            )
            for row in rows[:limit]
        )

        return results, len(rows) > limit

    async def recent_job_events(
        self,
        team_ids: tuple[UUID, ...],
        event_types: tuple[str, ...],
        *,
        limit: int,
    ) -> tuple[JobEvent, ...]:
        if not team_ids or not event_types:
            return ()

        rows = await self._pool.fetch(
            """
            SELECT
                event.id,
                event.job_id,
                event.event_type,
                event.sequence,
                event.created_at
              FROM portal_job_events AS event
              JOIN portal_jobs AS job ON job.id = event.job_id
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
            JobEvent(
                id=row["id"],
                job_id=row["job_id"],
                event_type=row["event_type"],
                sequence=int(row["sequence"]),
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

            job.items.append(
                JobItem(
                    id=item["id"],
                    ordinal=int(item["ordinal"]),
                    document=item["document"],
                    source=item["source"],
                    state=ItemState(item["state"]),
                    lease_fence=int(item["lease_fence"]),
                    result_object_id=item["result_object_id"],
                )
            )

        return job

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
            await self._lock_queue_gate(connection)
            await self._sweep_expired_locked(connection)

            row = await connection.fetchrow(
                _CLAIM_ONE,
                worker_id,
                list(sources),
            )

        if row is None:
            return None

        return ClaimedWork(
            item_id=row["id"],
            job_id=row["job_id"],
            source=row["source"],
            document=row["document"],
            lease_fence=int(row["lease_fence"]),
        )

    async def publish(
        self,
        item_id: UUID,
        worker_id: str,
        fence: int,
        result_object_id: UUID,
    ) -> bool:
        """Publish only while the worker still owns both fences."""

        async with self._pool.acquire() as connection, connection.transaction():
            await self._lock_queue_gate(connection)

            job_id = await connection.fetchval(
                _PUBLISH_FENCED,
                item_id,
                worker_id,
                fence,
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
        """Recover expired leases while holding the queue gate."""

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
