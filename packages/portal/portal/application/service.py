from __future__ import annotations

import hashlib

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from portal.domain.errors import NotFound, PermissionDenied
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
from portal.storage.port import ObjectReference
from portal.web.security import (
    new_csrf_token,
    new_session_token,
    token_hash,
    valid_csrf,
    verify_dummy_password,
    verify_password,
)


if TYPE_CHECKING:
    from uuid import UUID

    from portal.domain.models import CredentialVersion, Job, JobEvent
    from portal.repository.protocols import PortalRepository
    from portal.storage.port import ObjectStorage


class PortalService:
    """Team-scoped commands. Repository methods provide transaction boundaries."""

    def __init__(self, repository: PortalRepository) -> None:
        self._repository = repository

    async def submit(self, command: SubmitJob) -> Job:
        await self.require_leader(command.actor_id, command.team_id)
        credential = await self._repository.credential(command.credential_version_id)
        if credential is None or credential.team_id != command.team_id:
            raise PermissionDenied("la credencial debe pertenecer al mismo equipo")
        if not credential.is_active or credential.state is not CredentialState.ACTIVE:
            raise PermissionDenied("el equipo necesita una credencial proxy activa")
        return await self._repository.admit_submission(
            command, plan_submission(command.lines, command.sources)
        )

    async def cancel(self, actor_id: UUID, team_id: UUID, job_id: UUID) -> Job:
        await self.require_leader(actor_id, team_id)
        job = await self._repository.cancel(job_id, team_id)
        if job is None:
            raise NotFound("proceso no encontrado en el equipo")
        return job

    async def published_results(self, actor_id: UUID, team_id: UUID) -> tuple[Job, ...]:
        await self.require_reader(actor_id, team_id)
        return await self._repository.published_jobs(team_id)

    async def authenticate(self, email: str, password: str) -> PortalUser | None:
        found = await self._repository.user_by_email(email)
        if found is None:
            verify_dummy_password(password)
            return None
        if not verify_password(password, found[1]):
            return None
        return found[0]

    async def login(
        self, email: str, password: str, client_ip: str
    ) -> tuple[PortalUser, str] | None:
        now = datetime.now(UTC)
        # A rate-limited correct password receives the same generic response as a
        # bad one; verify it anyway to avoid an account-existence timing oracle.
        allowed = await self._repository.login_allowed(email, client_ip, now)
        user = await self.authenticate(email, password)
        if not allowed or user is None:
            await self._repository.record_login_failure(email, client_ip, now)
            return None
        await self._repository.clear_login_failures(email, client_ip)
        return user, await self.create_session(user.id)

    async def create_session(self, user_id: UUID) -> str:
        token = new_session_token()
        await self._repository.create_session(
            user_id,
            token_hash(token),
            new_csrf_token(),
            datetime.now(UTC) + timedelta(hours=12),
        )
        return token

    async def current_user(self, token: str | None) -> PortalUser | None:
        if not token:
            return None
        return await self._repository.session_user(token_hash(token), datetime.now(UTC))

    async def browser_session(self, token: str | None):
        if not token:
            return None
        return await self._repository.browser_session(
            token_hash(token), datetime.now(UTC)
        )

    async def verify_browser_csrf(
        self, token: str | None, submitted: str | None
    ) -> PortalUser:
        session = await self.browser_session(token)
        if session is None or not valid_csrf(submitted, session.csrf_token):
            raise PermissionDenied("la verificación CSRF no es válida")
        return session.user

    async def issue_login_csrf(self) -> str:
        token = new_csrf_token()
        await self._repository.issue_login_csrf(
            token, datetime.now(UTC) + timedelta(minutes=10)
        )
        return token

    async def consume_login_csrf(self, submitted: str | None) -> bool:
        return bool(
            submitted
            and await self._repository.consume_login_csrf(submitted, datetime.now(UTC))
        )

    async def destroy_session(self, token: str | None) -> None:
        if token:
            await self._repository.destroy_session(token_hash(token))

    async def teams(self, actor_id: UUID) -> tuple[Team, ...]:
        return await self._repository.teams_for_user(actor_id)

    async def team(self, actor_id: UUID, team_id: UUID) -> Team:
        await self.require_reader(actor_id, team_id)
        team = await self._repository.team(team_id)
        if team is None:
            raise NotFound("equipo no encontrado")
        role = await self._repository.role_for(actor_id, team_id)
        return Team(team.id, team.slug, team.name, role)

    async def jobs(
        self, actor_id: UUID, team_id: UUID, *, page: int, page_size: int = 20
    ) -> tuple[tuple[Job, ...], int]:
        await self.require_reader(actor_id, team_id)
        return await self._repository.jobs_for_team(
            team_id, page=page, page_size=page_size
        )

    async def job(self, actor_id: UUID, team_id: UUID, job_id: UUID) -> Job:
        await self.require_reader(actor_id, team_id)
        job = await self._repository.job(job_id, team_id)
        if job is None:
            raise NotFound("proceso no encontrado en el equipo")
        return job

    async def job_events_after(
        self, actor_id: UUID, team_id: UUID, job_id: UUID, sequence: int
    ) -> tuple[JobEvent, ...]:
        await self.job(actor_id, team_id, job_id)
        return await self._repository.job_events_after(job_id, team_id, sequence)

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
        return await self._repository.search_published(
            team_id, needle, limit=page_size, offset=(page - 1) * page_size
        )

    async def notifications(self, actor_id: UUID) -> tuple[JobEvent, ...]:
        teams = await self.teams(actor_id)
        return await self._repository.recent_job_events(
            tuple(team.id for team in teams), TERMINAL_JOB_EVENTS, limit=100
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
        """Persist an immutable submission and admit its already-parsed input lines."""
        await self.require_leader(actor_id, team_id)
        credential = await self._repository.credential(credential_version_id)
        if (
            credential is None
            or credential.team_id != team_id
            or not credential.is_active
            or credential.state is not CredentialState.ACTIVE
        ):
            msg = "el equipo necesita una credencial proxy activa"
            raise PermissionDenied(msg)
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
        await self._repository.add_object_reference(reference)
        return await self.submit(
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
        self, actor_id: UUID, team_id: UUID
    ) -> tuple[CredentialVersion, ...]:
        await self.require_leader(actor_id, team_id)
        return await self._repository.credentials_for_team(team_id)

    async def require_reader(self, actor_id: UUID, team_id: UUID) -> TeamRole:
        role = await self._repository.role_for(actor_id, team_id)
        if role is None:
            raise PermissionDenied("no pertenece al equipo")
        return role

    async def require_leader(self, actor_id: UUID, team_id: UUID) -> TeamRole:
        role = await self.require_reader(actor_id, team_id)
        if role is not TeamRole.TEAM_LEADER:
            raise PermissionDenied("solo un líder del equipo puede gestionar procesos")
        return role
