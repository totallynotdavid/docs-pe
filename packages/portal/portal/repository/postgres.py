from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from portal.domain.errors import SourceValidationError
from portal.domain.models import (
    MAX_ACTIVE_JOBS,
    STABLE_SOURCES,
    ClaimedWork,
    CredentialVersion,
    Job,
    JobState,
    SubmissionPlan,
    SubmitJob,
    TeamRole,
)


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
   SET state = 'running', started_at = now(), updated_at = now()
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
   SET state = 'running', lease_owner = $1, lease_fence = candidate.lease_fence,
       lease_expires_at = now() + interval '5 minutes', updated_at = now()
  FROM candidate
 WHERE item.id = candidate.id
RETURNING item.id, item.job_id, item.source, item.document, item.lease_fence
"""

_PUBLISH_FENCED = """
UPDATE portal_job_items AS item
   SET state = 'published', result_object_id = $4, published_at = now(),
       finished_at = now(), lease_owner = NULL, lease_expires_at = NULL,
       updated_at = now()
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


class PostgresPortalRepository:
    """PostgreSQL implementation of the team portal control plane.

    Every state change that admits, cancels, finishes, or promotes a job locks the
    singleton queue-control row in the same transaction. That makes the five active
    process limit exact across web and worker processes without a second queue.
    """

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def role_for(self, actor_id: UUID, team_id: UUID) -> TeamRole | None:
        async with self._pool.acquire() as connection:
            admin = await connection.fetchval(
                "SELECT is_site_admin FROM portal_users WHERE id = $1", actor_id
            )
            if admin:
                return TeamRole.SITE_ADMIN
            role = await connection.fetchval(
                """
                SELECT role FROM portal_team_memberships
                 WHERE team_id = $1 AND user_id = $2
                """,
                team_id,
                actor_id,
            )
        return TeamRole(role) if role else None

    async def credential(self, credential_version_id: UUID) -> CredentialVersion | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT version.id, version.team_id, credential.label,
                       version.version, version.is_active
                  FROM portal_team_proxy_credential_versions AS version
                  JOIN portal_team_proxy_credentials AS credential
                    ON credential.id = version.credential_id
                   AND credential.team_id = version.team_id
                 WHERE version.id = $1
                """,
                credential_version_id,
            )
        if row is None:
            return None
        return CredentialVersion(
            id=row["id"],
            team_id=row["team_id"],
            label=row["label"],
            version=int(row["version"]),
            is_active=bool(row["is_active"]),
        )

    async def admit_submission(self, command: SubmitJob, plan: SubmissionPlan) -> Job:
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
                    id, team_id, submitted_by, credential_version_id, input_object_id,
                    filename, sources, state, terminal_reason
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8,
                          CASE WHEN $8 = 'completed' THEN 'todos_los_registros_excluidos' END)
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
                INSERT INTO portal_job_items
                    (id, job_id, team_id, ordinal, document, source, state)
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
                INSERT INTO portal_job_items
                    (id, job_id, team_id, ordinal, document, source, state, reason)
                VALUES ($1, $2, $3, $4, $5, '', 'excluded', $6)
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
                await self._terminal_intents(connection, job_id, JobState.COMPLETED)
            else:
                await self._event(
                    connection, job_id, f"proceso.{state.value}", command.actor_id
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
                SELECT * FROM portal_jobs
                 WHERE id = $1 AND team_id = $2
                 FOR UPDATE
                """,
                job_id,
                team_id,
            )
            if row is None:
                return None
            state = JobState(row["state"])
            if state not in {JobState.QUEUED, JobState.RUNNING, JobState.CANCELLING}:
                return self._job(row)
            if state is JobState.RUNNING:
                await connection.execute(
                    """
                    UPDATE portal_jobs
                       SET state = 'cancelling', lease_fence = lease_fence + 1,
                           updated_at = now()
                     WHERE id = $1
                    """,
                    job_id,
                )
                await self._event(
                    connection, job_id, "proceso.cancelacion_solicitada", None
                )
            # Existing published references stay untouched; every active write now has
            # an obsolete fence and cannot publish a late result.
            await connection.execute(
                """
                UPDATE portal_job_items
                   SET state = 'cancelled', finished_at = now()
                 WHERE job_id = $1 AND state IN ('pending', 'running')
                """,
                job_id,
            )
            row = await connection.fetchrow(
                """
                UPDATE portal_jobs
                   SET state = 'cancelled', finished_at = now(), updated_at = now()
                 WHERE id = $1
                RETURNING *
                """,
                job_id,
            )
            await self._terminal_intents(connection, job_id, JobState.CANCELLED)
            await self._promote_locked(connection)
        return self._job(row)

    async def published_jobs(self, team_id: UUID) -> tuple[Job, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT DISTINCT job.*
                  FROM portal_jobs job
                  JOIN portal_job_items item ON item.job_id = job.id
                 WHERE job.team_id = $1 AND item.state = 'published'
                 ORDER BY job.queue_sequence
                """,
                team_id,
            )
        return tuple(self._job(row) for row in rows)

    async def claim(
        self, worker_id: str, sources: tuple[str, ...]
    ) -> ClaimedWork | None:
        """Lease the earliest eligible item while holding the cancellation gate."""
        if not sources:
            msg = "el trabajador debe declarar al menos una fuente"
            raise SourceValidationError(msg)
        invalid = sorted(set(sources).difference(STABLE_SOURCES))
        if invalid:
            msg = f"fuentes no habilitadas: {', '.join(invalid)}"
            raise SourceValidationError(msg)
        async with self._pool.acquire() as connection, connection.transaction():
            await self._lock_queue_gate(connection)
            row = await connection.fetchrow(_CLAIM_ONE, worker_id, list(sources))
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
        self, item_id: UUID, worker_id: str, fence: int, result_object_id: UUID
    ) -> bool:
        """Persist a result only if the current item and job fences still match."""
        async with self._pool.acquire() as connection, connection.transaction():
            await self._lock_queue_gate(connection)
            job_id = await connection.fetchval(
                _PUBLISH_FENCED, item_id, worker_id, fence, result_object_id
            )
            if job_id is None:
                return False
            await self._finish_if_drained_locked(connection, job_id)
        return True

    async def _lock_queue_gate(self, connection: Connection) -> int:
        maximum = await connection.fetchval(_LOCK_QUEUE_GATE)
        if maximum != MAX_ACTIVE_JOBS:
            msg = "portal_queue_control must retain the fixed five-process limit"
            raise RuntimeError(msg)
        return int(maximum)

    async def _promote_locked(self, connection: Connection) -> None:
        active = int(await connection.fetchval(_ACTIVE_COUNT))
        slots = MAX_ACTIVE_JOBS - active
        if slots > 0:
            promoted = await connection.fetch(_PROMOTE_FIFO, slots)
            for row in promoted:
                await self._event(connection, row["id"], "proceso.running", None)

    async def _finish_if_drained_locked(
        self, connection: Connection, job_id: UUID
    ) -> None:
        finished = await connection.fetchval(
            """
            UPDATE portal_jobs
               SET state = 'completed', finished_at = now(), updated_at = now()
             WHERE id = $1
               AND state = 'running'
               AND NOT EXISTS (
                   SELECT 1 FROM portal_job_items
                    WHERE job_id = $1 AND state IN ('pending', 'running')
               )
            RETURNING id
            """,
            job_id,
        )
        if finished is not None:
            await self._terminal_intents(connection, job_id, JobState.COMPLETED)
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
            INSERT INTO portal_job_events (id, job_id, event_type, actor_id)
            VALUES ($1, $2, $3, $4)
            """,
            event_id,
            job_id,
            event_type,
            actor_id,
        )
        return event_id

    async def _terminal_intents(
        self, connection: Connection, job_id: UUID, state: JobState
    ) -> None:
        event_id = await self._event(connection, job_id, f"proceso.{state.value}", None)
        await connection.executemany(
            """
            INSERT INTO portal_notification_outbox (id, event_id, channel, payload)
            VALUES ($1, $2, $3, jsonb_build_object('job_id', $4::text, 'estado', $5::text))
            """,
            [
                (uuid4(), event_id, channel, str(job_id), state.value)
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
        )
