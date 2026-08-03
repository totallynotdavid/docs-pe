from __future__ import annotations

import hashlib

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from portal.domain.errors import NotFound, PermissionDenied, Reason
from portal.domain.models import (
    TERMINAL_JOB_EVENTS,
    CredentialState,
    InputLine,
    PortalUser,
    SearchResult,
    SubmitJob,
    Team,
    TeamRole,
)
from portal.domain.planning import plan_submission
from portal.security import (
    new_csrf_token,
    new_session_token,
    token_hash,
    valid_csrf,
    verify_dummy_password,
    verify_password,
)
from portal.storage.port import ObjectReference


if TYPE_CHECKING:
    from uuid import UUID

    from portal.domain.models import (
        BrowserSession,
        CredentialVersion,
        Job,
        JobEvent,
    )
    from portal.repository.auth import PostgresAuthRepository
    from portal.repository.credentials import PostgresCredentialRepository
    from portal.repository.jobs import PostgresJobRepository
    from portal.repository.teams import PostgresTeamRepository
    from portal.storage.port import ObjectStorage


class PortalService:
    def __init__(
        self,
        auth: PostgresAuthRepository,
        teams: PostgresTeamRepository,
        credentials: PostgresCredentialRepository,
        jobs: PostgresJobRepository,
    ) -> None:
        self._auth = auth
        self._teams = teams
        self._credentials = credentials
        self._jobs = jobs

    async def submit(self, command: SubmitJob) -> Job:
        await self.require_leader(command.actor_id, command.team_id)
        await self._require_active_credential(
            command.credential_version_id,
            command.team_id,
        )

        return await self._admit(command)

    async def cancel(
        self,
        actor_id: UUID,
        team_id: UUID,
        job_id: UUID,
    ) -> Job:
        await self.require_leader(actor_id, team_id)

        job = await self._jobs.cancel(job_id, team_id)

        if job is None:
            raise NotFound(Reason.JOB_NOT_FOUND)

        return job

    async def published_results(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> tuple[Job, ...]:
        await self.require_reader(actor_id, team_id)

        return await self._jobs.published_jobs(team_id)

    async def authenticate(
        self,
        email: str,
        password: str,
    ) -> PortalUser | None:
        found = await self._auth.user_by_email(email)

        if found is None:
            verify_dummy_password(password)
            return None

        user, password_hash = found

        if not verify_password(password, password_hash):
            return None

        return user

    async def login(
        self,
        email: str,
        password: str,
        client_ip: str,
    ) -> tuple[PortalUser, str] | None:
        now = datetime.now(UTC)

        # Always verify the password to avoid a rate-limit timing oracle.
        allowed = await self._auth.login_allowed(email, client_ip, now)
        user = await self.authenticate(email, password)

        if not allowed or user is None:
            await self._auth.record_login_failure(email, client_ip, now)
            return None

        await self._auth.clear_login_failures(email, client_ip)

        return user, await self.create_session(user.id)

    async def create_session(self, user_id: UUID) -> str:
        token = new_session_token()

        await self._auth.create_session(
            user_id,
            token_hash(token),
            new_csrf_token(),
            datetime.now(UTC) + timedelta(hours=12),
        )

        return token

    async def browser_session(
        self,
        token: str | None,
    ) -> BrowserSession | None:
        if not token:
            return None

        return await self._auth.browser_session(
            token_hash(token),
            datetime.now(UTC),
        )

    async def verify_browser_csrf(
        self,
        token: str | None,
        submitted: str | None,
    ) -> BrowserSession:
        session = await self.browser_session(token)

        if session is None or not valid_csrf(submitted, session.csrf_token):
            raise PermissionDenied(Reason.CSRF_INVALID)

        return session

    async def issue_login_csrf(self) -> str:
        token = new_csrf_token()

        await self._auth.issue_login_csrf(
            token,
            datetime.now(UTC) + timedelta(minutes=10),
        )

        return token

    async def consume_login_csrf(self, submitted: str | None) -> bool:
        return bool(
            submitted
            and await self._auth.consume_login_csrf(
                submitted,
                datetime.now(UTC),
            )
        )

    async def destroy_session(self, token: str | None) -> None:
        if token:
            await self._auth.destroy_session(token_hash(token))

    async def teams(self, actor_id: UUID) -> tuple[Team, ...]:
        return await self._teams.teams_for_user(actor_id)

    async def team(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> Team:
        role = await self.require_reader(actor_id, team_id)
        team = await self._teams.team(team_id)

        if team is None:
            raise NotFound(Reason.TEAM_NOT_FOUND)

        return Team(team.id, team.slug, team.name, role)

    async def jobs(
        self,
        actor_id: UUID,
        team_id: UUID,
        *,
        page: int,
        page_size: int = 20,
    ) -> tuple[tuple[Job, ...], int]:
        await self.require_reader(actor_id, team_id)

        return await self._jobs.jobs_for_team(
            team_id,
            page=page,
            page_size=page_size,
        )

    async def job(
        self,
        actor_id: UUID,
        team_id: UUID,
        job_id: UUID,
    ) -> Job:
        await self.require_reader(actor_id, team_id)

        job = await self._jobs.job(job_id, team_id)

        if job is None:
            raise NotFound(Reason.JOB_NOT_FOUND)

        return job

    async def job_events_after(
        self,
        actor_id: UUID,
        team_id: UUID,
        job_id: UUID,
        sequence: int,
    ) -> tuple[JobEvent, ...]:
        await self.job(actor_id, team_id, job_id)

        return await self._jobs.job_events_after(
            job_id,
            team_id,
            sequence,
        )

    async def search(
        self,
        actor_id: UUID,
        team_id: UUID,
        query: str,
        *,
        page: int,
        page_size: int = 20,
    ) -> tuple[tuple[SearchResult, ...], bool]:
        await self.require_reader(actor_id, team_id)

        needle = query.strip()

        if not needle:
            return (), False

        return await self._jobs.search_published(
            team_id,
            needle,
            limit=page_size,
            offset=(page - 1) * page_size,
        )

    async def notifications(
        self,
        actor_id: UUID,
    ) -> tuple[JobEvent, ...]:
        teams = await self.teams(actor_id)

        return await self._jobs.recent_job_events(
            tuple(team.id for team in teams),
            TERMINAL_JOB_EVENTS,
            limit=100,
        )

    async def submit_input(
        self,
        *,
        actor_id: UUID,
        team_id: UUID,
        credential_version_id: UUID,
        filename: str,
        content: bytes,
        content_type: str,
        lines: tuple[InputLine, ...],
        sources: tuple[str, ...],
        storage: ObjectStorage,
    ) -> Job:
        await self.require_leader(actor_id, team_id)
        await self._require_active_credential(
            credential_version_id,
            team_id,
        )

        reference = ObjectReference(
            id=uuid4(),
            team_id=team_id,
            provider="portal-browser",
            container="submissions",
            object_key=f"{team_id}/{uuid4()}",
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            content_type=content_type,
        )

        await storage.put_immutable(reference, content)
        await self._jobs.add_object_reference(reference)

        return await self._admit(
            SubmitJob(
                actor_id=actor_id,
                team_id=team_id,
                credential_version_id=credential_version_id,
                input_object_id=reference.id,
                filename=filename,
                sources=sources,
                lines=lines,
            )
        )

    async def credentials(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> tuple[CredentialVersion, ...]:
        await self.require_leader(actor_id, team_id)

        return await self._credentials.credentials_for_team(team_id)

    async def require_reader(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> TeamRole:
        role = await self._teams.role_for(actor_id, team_id)

        if role is None:
            raise PermissionDenied(Reason.NOT_A_MEMBER)

        return role

    async def require_leader(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> TeamRole:
        role = await self.require_reader(actor_id, team_id)

        if role is not TeamRole.TEAM_LEADER:
            raise PermissionDenied(Reason.LEADER_REQUIRED)

        return role

    async def _require_active_credential(
        self,
        credential_version_id: UUID,
        team_id: UUID,
    ) -> CredentialVersion:
        credential = await self._credentials.credential(credential_version_id)

        if credential is None or credential.team_id != team_id:
            raise PermissionDenied(Reason.CREDENTIAL_WRONG_TEAM)

        if not credential.is_active or credential.state is not CredentialState.ACTIVE:
            raise PermissionDenied(Reason.CREDENTIAL_REQUIRED)

        return credential

    async def _admit(self, command: SubmitJob) -> Job:
        return await self._jobs.admit_submission(
            command,
            plan_submission(command.lines, command.sources),
        )
