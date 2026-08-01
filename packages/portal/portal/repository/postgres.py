from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from portal.domain.errors import SourceValidationError
from portal.domain.models import (
    MAX_ACTIVE_JOBS,
    MAX_LEASE_ATTEMPTS,
    STABLE_SOURCES,
    BrowserSession,
    ClaimedWork,
    CredentialState,
    CredentialVersion,
    ExcludedInput,
    ItemState,
    Job,
    JobCredential,
    JobEvent,
    JobItem,
    JobState,
    PortalUser,
    ProxyProvider,
    SearchResult,
    SubmissionPlan,
    SubmitJob,
    Team,
    TeamRole,
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
       lease_expires_at = now() + interval '5 minutes', attempts = item.attempts + 1,
       updated_at = now()
  FROM candidate
 WHERE item.id = candidate.id
RETURNING item.id, item.job_id, item.source, item.document, item.lease_fence
"""

# Cancellation retires its own items in a single transaction, so an expired lease
# only ever belongs to a running job. Items of a job that left 'running' are
# already terminal and must not be resurrected here.
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
   SET state = CASE WHEN expired.attempts >= $1 THEN 'failed' ELSE 'pending' END,
       reason = CASE WHEN expired.attempts >= $1 THEN 'lease_expired' ELSE item.reason END,
       finished_at = CASE WHEN expired.attempts >= $1 THEN now() ELSE NULL END,
       lease_owner = NULL, lease_expires_at = NULL, updated_at = now()
  FROM expired
 WHERE item.id = expired.id
RETURNING item.job_id
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
                       version.version, version.is_active, version.lifecycle,
                       version.provider
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

    async def search_published(
        self, team_id: UUID, needle: str, *, limit: int, offset: int
    ) -> tuple[tuple[SearchResult, ...], bool]:
        """Match published documents by substring, newest job first.

        Fetches one row beyond the page so the caller learns whether another page
        exists without counting the whole match set.
        """
        rows = await self._pool.fetch(
            """
            SELECT item.job_id, job.filename, item.document
              FROM portal_job_items AS item
              JOIN portal_jobs AS job ON job.id = item.job_id
             WHERE item.team_id = $1
               AND item.state = 'published'
               AND item.document ILIKE '%' || $2 || '%'
             ORDER BY job.queue_sequence DESC, item.ordinal
             LIMIT $3 OFFSET $4
            """,
            team_id,
            needle,
            limit + 1,
            offset,
        )
        results = tuple(
            SearchResult(row["job_id"], row["filename"], row["document"])
            for row in rows[:limit]
        )
        return results, len(rows) > limit

    async def recent_job_events(
        self, team_ids: tuple[UUID, ...], event_types: tuple[str, ...], *, limit: int
    ) -> tuple[JobEvent, ...]:
        if not team_ids or not event_types:
            return ()
        rows = await self._pool.fetch(
            """
            SELECT event.id, event.job_id, event.event_type, event.sequence,
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
                  FROM portal_jobs job
                  JOIN portal_job_items item ON item.job_id = job.id
                 WHERE job.team_id = $1 AND item.state = 'published'
                 ORDER BY job.queue_sequence
                """,
                team_id,
            )
        return tuple(self._job(row) for row in rows)

    async def user_by_email(self, email: str) -> tuple[PortalUser, str] | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT id, email, is_site_admin, password_hash
                  FROM portal_users WHERE email = $1
                """,
                email.lower().strip(),
            )
        if row is None:
            return None
        return self._user(row), str(row["password_hash"])

    async def user_by_id(self, user_id: UUID) -> PortalUser | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT id, email, is_site_admin FROM portal_users WHERE id = $1",
                user_id,
            )
        return self._user(row) if row else None

    async def create_user(
        self, email: str, password_hash: str, *, is_site_admin: bool = False
    ) -> PortalUser:
        user = PortalUser(uuid4(), email.lower().strip(), is_site_admin)
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO portal_users (id, email, password_hash, is_site_admin)
                VALUES ($1, $2, $3, $4)
                """,
                user.id,
                user.email,
                password_hash,
                user.is_site_admin,
            )
        return user

    async def provision_site_admin(self, email: str, password_hash: str) -> PortalUser:
        """Create/find the declared initial administrator without changing a password."""
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO portal_users (id, email, password_hash, is_site_admin)
                VALUES ($1, $2, $3, true)
                ON CONFLICT (email) DO UPDATE
                    SET is_site_admin = true
                RETURNING id, email, is_site_admin
                """,
                uuid4(),
                email.lower().strip(),
                password_hash,
            )
        return self._user(row)

    async def create_session(
        self, user_id: UUID, token_hash: str, csrf_token: str, expires_at: datetime
    ) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO portal_sessions (id, user_id, token_hash, csrf_token, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                uuid4(),
                user_id,
                token_hash,
                csrf_token,
                expires_at,
            )

    async def session_user(self, token_hash: str, now: datetime) -> PortalUser | None:
        session = await self.browser_session(token_hash, now)
        return session.user if session else None

    async def browser_session(
        self, token_hash: str, now: datetime
    ) -> BrowserSession | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT user_account.id, user_account.email, user_account.is_site_admin,
                       session.csrf_token
                  FROM portal_sessions AS session
                  JOIN portal_users AS user_account ON user_account.id = session.user_id
                 WHERE session.token_hash = $1
                   AND session.expires_at > $2
                   AND session.csrf_token IS NOT NULL
                """,
                token_hash,
                now,
            )
            await connection.execute(
                "DELETE FROM portal_sessions WHERE expires_at <= $1", now
            )
        if row is None:
            return None
        return BrowserSession(self._user(row), str(row["csrf_token"]))

    async def destroy_session(self, token_hash: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM portal_sessions WHERE token_hash = $1", token_hash
            )

    async def issue_login_csrf(self, token: str, expires_at: datetime) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO portal_login_csrf_tokens (token, expires_at) VALUES ($1, $2)",
                token,
                expires_at,
            )

    async def consume_login_csrf(self, token: str, now: datetime) -> bool:
        async with self._pool.acquire() as connection:
            consumed = await connection.fetchval(
                """
                DELETE FROM portal_login_csrf_tokens
                 WHERE token = $1 AND expires_at > $2
                RETURNING token
                """,
                token,
                now,
            )
            await connection.execute(
                "DELETE FROM portal_login_csrf_tokens WHERE expires_at <= $1", now
            )
        return consumed is not None

    async def login_allowed(self, email: str, client_ip: str, now: datetime) -> bool:
        async with self._pool.acquire() as connection:
            failures = await connection.fetchval(
                """
                SELECT count(*) FROM portal_login_failures
                 WHERE email = $1 AND client_ip = $2
                   AND attempted_at > $3::timestamptz - interval '5 minutes'
                """,
                email.lower().strip(),
                client_ip,
                now,
            )
        return int(failures) < 5

    async def record_login_failure(
        self, email: str, client_ip: str, now: datetime
    ) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO portal_login_failures (email, client_ip, attempted_at)
                VALUES ($1, $2, $3)
                """,
                email.lower().strip(),
                client_ip,
                now,
            )

    async def clear_login_failures(self, email: str, client_ip: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM portal_login_failures WHERE email = $1 AND client_ip = $2",
                email.lower().strip(),
                client_ip,
            )

    async def teams_for_user(self, actor_id: UUID) -> tuple[Team, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT team.id, team.slug, team.name, membership.role
                  FROM portal_teams AS team
                  JOIN portal_team_memberships AS membership ON membership.team_id = team.id
                 WHERE membership.user_id = $1
                 ORDER BY team.name
                """,
                actor_id,
            )
        return tuple(
            Team(row["id"], row["slug"], row["name"], TeamRole(row["role"]))
            for row in rows
        )

    async def team(self, team_id: UUID) -> Team | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT id, slug, name FROM portal_teams WHERE id = $1", team_id
            )
        return Team(row["id"], row["slug"], row["name"]) if row else None

    async def team_by_slug(self, slug: str) -> Team | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT id, slug, name FROM portal_teams WHERE slug = $1", slug
            )
        return Team(row["id"], row["slug"], row["name"]) if row else None

    async def all_teams(self) -> tuple[Team, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT id, slug, name FROM portal_teams ORDER BY name"
            )
        return tuple(Team(row["id"], row["slug"], row["name"]) for row in rows)

    async def users(self) -> tuple[PortalUser, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT id, email, is_site_admin FROM portal_users ORDER BY email"
            )
        return tuple(self._user(row) for row in rows)

    async def installation_status(self) -> tuple[int, UUID | None]:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT initial_team_id FROM portal_installation_state WHERE singleton = true"
            )
            count = await connection.fetchval("SELECT count(*) FROM portal_teams")
        return int(count), row["initial_team_id"] if row else None

    async def create_first_team(self, slug: str, name: str, actor_id: UUID) -> Team:
        team = Team(uuid4(), slug, name)
        async with self._pool.acquire() as connection, connection.transaction():
            state = await connection.fetchrow(
                """
                SELECT initial_team_id FROM portal_installation_state
                 WHERE singleton = true FOR UPDATE
                """
            )
            count = await connection.fetchval("SELECT count(*) FROM portal_teams")
            if state is None or state["initial_team_id"] is not None or int(count):
                msg = "la instalación ya tiene un equipo inicial"
                raise ValueError(msg)
            await self._insert_team_and_leader(connection, team, actor_id, actor_id)
            await connection.execute(
                """
                UPDATE portal_installation_state
                   SET initial_team_id = $1, completed_by = $2,
                       completed_at = now(), updated_at = now()
                 WHERE singleton = true
                """,
                team.id,
                actor_id,
            )
        return team

    async def create_team(
        self, slug: str, name: str, created_by: UUID, leader_id: UUID
    ) -> Team:
        team = Team(uuid4(), slug, name)
        async with self._pool.acquire() as connection, connection.transaction():
            await self._insert_team_and_leader(connection, team, created_by, leader_id)
        return team

    async def add_member(self, team_id: UUID, user_id: UUID, role: TeamRole) -> None:
        if role not in {TeamRole.TEAM_LEADER, TeamRole.TEAM_MEMBER}:
            msg = "el rol de equipo no es válido"
            raise ValueError(msg)
        async with self._pool.acquire() as connection, connection.transaction():
            await self._lock_team_memberships(connection, team_id)
            current = await connection.fetchval(
                """
                SELECT role FROM portal_team_memberships
                 WHERE team_id = $1 AND user_id = $2 FOR UPDATE
                """,
                team_id,
                user_id,
            )
            if (
                current == TeamRole.TEAM_LEADER.value
                and role is not TeamRole.TEAM_LEADER
            ):
                await self._ensure_not_last_leader(connection, team_id)
            await connection.execute(
                """
                INSERT INTO portal_team_memberships (team_id, user_id, role)
                VALUES ($1, $2, $3)
                ON CONFLICT (team_id, user_id) DO UPDATE SET role = EXCLUDED.role
                """,
                team_id,
                user_id,
                role.value,
            )

    async def remove_member(self, team_id: UUID, user_id: UUID) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            await self._lock_team_memberships(connection, team_id)
            current = await connection.fetchval(
                """
                SELECT role FROM portal_team_memberships
                 WHERE team_id = $1 AND user_id = $2 FOR UPDATE
                """,
                team_id,
                user_id,
            )
            if current == TeamRole.TEAM_LEADER.value:
                await self._ensure_not_last_leader(connection, team_id)
            await connection.execute(
                "DELETE FROM portal_team_memberships WHERE team_id = $1 AND user_id = $2",
                team_id,
                user_id,
            )

    async def members_for_team(
        self, team_id: UUID
    ) -> tuple[tuple[PortalUser, TeamRole], ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT user_account.id, user_account.email, user_account.is_site_admin,
                       membership.role
                  FROM portal_team_memberships AS membership
                  JOIN portal_users AS user_account ON user_account.id = membership.user_id
                 WHERE membership.team_id = $1
                 ORDER BY user_account.email
                """,
                team_id,
            )
        return tuple((self._user(row), TeamRole(row["role"])) for row in rows)

    async def credentials_for_team(
        self, team_id: UUID
    ) -> tuple[CredentialVersion, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT version.id, version.team_id, credential.label,
                       version.version, version.is_active, version.lifecycle,
                       version.provider
                  FROM portal_team_proxy_credential_versions AS version
                  JOIN portal_team_proxy_credentials AS credential
                    ON credential.id = version.credential_id
                 WHERE version.team_id = $1
                 ORDER BY credential.label, version.version DESC
                """,
                team_id,
            )
        return tuple(self._credential(row) for row in rows)

    async def start_credential_validation(
        self,
        team_id: UUID,
        label: str,
        provider: str,
        config_ciphertext: bytes,
        key_id: str,
        created_by: UUID,
    ) -> CredentialVersion:
        async with self._pool.acquire() as connection, connection.transaction():
            await self._lock_team_memberships(connection, team_id)
            credential_id = await connection.fetchval(
                """
                SELECT id FROM portal_team_proxy_credentials
                 WHERE team_id = $1 AND label = $2 FOR UPDATE
                """,
                team_id,
                label,
            )
            if credential_id is None:
                credential_id = uuid4()
                await connection.execute(
                    """
                    INSERT INTO portal_team_proxy_credentials (id, team_id, label, created_by)
                    VALUES ($1, $2, $3, $4)
                    """,
                    credential_id,
                    team_id,
                    label,
                    created_by,
                )
            version = int(
                await connection.fetchval(
                    """
                    SELECT COALESCE(max(version), 0) + 1
                      FROM portal_team_proxy_credential_versions
                     WHERE credential_id = $1
                    """,
                    credential_id,
                )
            )
            credential = CredentialVersion(
                uuid4(),
                team_id,
                label,
                version,
                is_active=False,
                state=CredentialState.VALIDATING,
                provider=ProxyProvider(provider),
            )
            await connection.execute(
                """
                INSERT INTO portal_team_proxy_credential_versions
                    (id, credential_id, team_id, version, provider, config_ciphertext,
                     key_id, lifecycle, is_active, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'validating', false, $8)
                """,
                credential.id,
                credential_id,
                team_id,
                credential.version,
                provider,
                config_ciphertext,
                key_id,
                created_by,
            )
            await connection.execute(
                """
                INSERT INTO portal_proxy_credential_events
                    (id, credential_version_id, from_lifecycle, to_lifecycle, detail, actor_id)
                VALUES ($1, $2, 'draft', 'validating', 'validación iniciada', $3)
                """,
                uuid4(),
                credential.id,
                created_by,
            )
        return credential

    async def finish_credential_validation(
        self,
        credential_version_id: UUID,
        *,
        state: CredentialState,
        detail: str | None,
        actor_id: UUID,
    ) -> CredentialVersion:
        if state not in {CredentialState.ACTIVE, CredentialState.FAILED}:
            msg = "el estado final de la credencial no es válido"
            raise ValueError(msg)
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                SELECT version.id, version.team_id, credential.id AS credential_id,
                       credential.label, version.version, version.is_active,
                       version.lifecycle, version.provider
                  FROM portal_team_proxy_credential_versions AS version
                  JOIN portal_team_proxy_credentials AS credential
                    ON credential.id = version.credential_id
                   AND credential.team_id = version.team_id
                 WHERE version.id = $1
                 FOR UPDATE OF credential, version
                """,
                credential_version_id,
            )
            if row is None or row["lifecycle"] != CredentialState.VALIDATING.value:
                msg = "la credencial no está pendiente de validación"
                raise ValueError(msg)
            if state is CredentialState.ACTIVE:
                retired = await connection.fetch(
                    """
                    UPDATE portal_team_proxy_credential_versions
                       SET lifecycle = 'retired', is_active = false
                     WHERE credential_id = $1 AND lifecycle = 'active'
                    RETURNING id
                    """,
                    row["credential_id"],
                )
                await connection.executemany(
                    """
                    INSERT INTO portal_proxy_credential_events
                        (id, credential_version_id, from_lifecycle, to_lifecycle, detail, actor_id)
                    VALUES ($1, $2, 'active', 'retired', 'reemplazada por una nueva versión', $3)
                    """,
                    [(uuid4(), retired_row["id"], actor_id) for retired_row in retired],
                )
            await connection.execute(
                """
                UPDATE portal_team_proxy_credential_versions
                   SET lifecycle = $2, is_active = $3,
                       validated_at = CASE WHEN $2 = 'active' THEN now() ELSE validated_at END,
                       failure_detail = CASE WHEN $2 = 'failed' THEN $4 ELSE NULL END
                 WHERE id = $1
                """,
                credential_version_id,
                state.value,
                state is CredentialState.ACTIVE,
                detail,
            )
            await connection.execute(
                """
                INSERT INTO portal_proxy_credential_events
                    (id, credential_version_id, from_lifecycle, to_lifecycle, detail, actor_id)
                VALUES ($1, $2, 'validating', $3, $4, $5)
                """,
                uuid4(),
                credential_version_id,
                state.value,
                detail,
                actor_id,
            )
        return CredentialVersion(
            id=row["id"],
            team_id=row["team_id"],
            label=row["label"],
            version=int(row["version"]),
            is_active=state is CredentialState.ACTIVE,
            state=state,
            provider=ProxyProvider(row["provider"]),
        )

    async def add_object_reference(self, reference: ObjectReference) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO portal_object_references
                    (id, team_id, provider, container, object_key, sha256, size_bytes,
                     content_type)
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
        self, team_id: UUID, *, page: int, page_size: int
    ) -> tuple[tuple[Job, ...], int]:
        offset = (page - 1) * page_size
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT * FROM portal_jobs WHERE team_id = $1
                 ORDER BY queue_sequence DESC LIMIT $2 OFFSET $3
                """,
                team_id,
                page_size,
                offset,
            )
            total = await connection.fetchval(
                "SELECT count(*) FROM portal_jobs WHERE team_id = $1", team_id
            )
        return tuple(self._job(row) for row in rows), int(total)

    async def job(self, job_id: UUID, team_id: UUID) -> Job | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM portal_jobs WHERE id = $1 AND team_id = $2",
                job_id,
                team_id,
            )
            if row is None:
                return None
            item_rows = await connection.fetch(
                """
                SELECT id, ordinal, document, source, state, lease_fence, result_object_id, reason
                  FROM portal_job_items WHERE job_id = $1 ORDER BY ordinal, source
                """,
                job_id,
            )
        job = self._job(row)
        for item in item_rows:
            if item["state"] == "excluded":
                job.exclusions.append(
                    ExcludedInput(
                        int(item["ordinal"]), item["document"], item["reason"]
                    )
                )
            else:
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
        self, job_id: UUID, team_id: UUID, sequence: int
    ) -> tuple[JobEvent, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT event.id, event.job_id, event.event_type, event.sequence, event.created_at
                  FROM portal_job_events AS event
                  JOIN portal_jobs AS job ON job.id = event.job_id
                 WHERE event.job_id = $1 AND job.team_id = $2 AND event.sequence > $3
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
            await self._sweep_expired_locked(connection)
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

    async def credential_for_job(self, job_id: UUID) -> JobCredential | None:
        row = await self._pool.fetchrow(
            """
            SELECT version.provider, version.config_ciphertext
              FROM portal_jobs AS job
              JOIN portal_team_proxy_credential_versions AS version
                ON version.id = job.credential_version_id
             WHERE job.id = $1
            """,
            job_id,
        )
        if row is None:
            return None
        return JobCredential(row["provider"], bytes(row["config_ciphertext"]))

    async def item_team(self, item_id: UUID) -> UUID | None:
        team_id: UUID | None = await self._pool.fetchval(
            "SELECT team_id FROM portal_job_items WHERE id = $1", item_id
        )
        return team_id

    async def _insert_team_and_leader(
        self, connection: Connection, team: Team, created_by: UUID, leader_id: UUID
    ) -> None:
        """Keep the initial leader insertion in the same transaction as the team."""
        await connection.execute(
            """
            INSERT INTO portal_teams (id, slug, name, created_by)
            VALUES ($1, $2, $3, $4)
            """,
            team.id,
            team.slug,
            team.name,
            created_by,
        )
        await connection.execute(
            """
            INSERT INTO portal_team_memberships (team_id, user_id, role)
            VALUES ($1, $2, 'team_leader')
            """,
            team.id,
            leader_id,
        )

    async def _lock_team_memberships(
        self, connection: Connection, team_id: UUID
    ) -> None:
        """Serialize leader changes per team; the deferred SQL trigger is the backstop."""
        found = await connection.fetchval(
            "SELECT id FROM portal_teams WHERE id = $1 FOR UPDATE", team_id
        )
        if found is None:
            msg = "el equipo no existe"
            raise ValueError(msg)

    async def _ensure_not_last_leader(
        self, connection: Connection, team_id: UUID
    ) -> None:
        leaders = await connection.fetchval(
            """
            SELECT count(*) FROM portal_team_memberships
             WHERE team_id = $1 AND role = 'team_leader'
            """,
            team_id,
        )
        if int(leaders) <= 1:
            msg = "el equipo debe conservar al menos una persona líder"
            raise ValueError(msg)

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

    async def _sweep_expired_locked(self, connection: Connection) -> None:
        """Recover items whose worker stopped renewing the lease.

        Runs in the claim transaction rather than a reaper process: every queue
        transition already serializes on the queue-control gate, so a second
        writer would contend for the same row and stall the queue when it died.
        """
        rows = await connection.fetch(_SWEEP_EXPIRED, MAX_LEASE_ATTEMPTS)
        for job_id in {row["job_id"] for row in rows}:
            await self._finish_if_drained_locked(connection, job_id)

    async def _finish_if_drained_locked(
        self, connection: Connection, job_id: UUID
    ) -> None:
        # A job that drained without publishing anything did not succeed, so it
        # retires as failed rather than reporting an empty result as completed.
        row = await connection.fetchrow(
            """
            UPDATE portal_jobs AS job
               SET state = CASE
                       WHEN EXISTS (
                           SELECT 1 FROM portal_job_items
                            WHERE job_id = $1 AND state = 'published'
                       ) THEN 'completed' ELSE 'failed' END,
                   terminal_reason = CASE
                       WHEN EXISTS (
                           SELECT 1 FROM portal_job_items
                            WHERE job_id = $1 AND state = 'published'
                       ) THEN job.terminal_reason ELSE 'sin_resultados' END,
                   finished_at = now(), updated_at = now()
             WHERE job.id = $1
               AND job.state = 'running'
               AND NOT EXISTS (
                   SELECT 1 FROM portal_job_items
                    WHERE job_id = $1 AND state IN ('pending', 'running')
               )
            RETURNING job.state
            """,
            job_id,
        )
        if row is not None:
            await self._terminal_intents(connection, job_id, JobState(row["state"]))
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
            terminal_reason=row["terminal_reason"],  # type: ignore[index]
            created_at=row["created_at"],  # type: ignore[index]
        )

    @staticmethod
    def _user(row: object) -> PortalUser:
        return PortalUser(
            id=row["id"],  # type: ignore[index]
            email=row["email"],  # type: ignore[index]
            is_site_admin=bool(row["is_site_admin"]),  # type: ignore[index]
        )

    @staticmethod
    def _credential(row: object) -> CredentialVersion:
        return CredentialVersion(
            id=row["id"],  # type: ignore[index]
            team_id=row["team_id"],  # type: ignore[index]
            label=row["label"],  # type: ignore[index]
            version=int(row["version"]),  # type: ignore[index]
            is_active=bool(row["is_active"]),  # type: ignore[index]
            state=CredentialState(row["lifecycle"]),  # type: ignore[index]
            provider=ProxyProvider(row["provider"]),  # type: ignore[index]
        )
