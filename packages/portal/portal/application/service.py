from __future__ import annotations

import hashlib

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from portal.domain.errors import NotFound, PermissionDenied
from portal.domain.models import (
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
    hash_password,
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
        if not credential.is_active:
            raise PermissionDenied("la versión de credencial ya no está activa")
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

    async def bootstrap_site_admin(self, email: str, password: str) -> PortalUser:
        if not email or not password:
            msg = "el administrador inicial requiere correo y contraseña"
            raise ValueError(msg)
        return await self._repository.bootstrap_site_admin(
            email, hash_password(password)
        )

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
        needle = query.strip().lower()
        if not needle:
            return (), False
        published = await self._repository.published_jobs(team_id)
        results: list[SearchResult] = []
        for listed in published:
            job = await self._repository.job(listed.id, team_id)
            if job is None:
                continue
            results.extend(
                SearchResult(job.id, job.filename, item.document)
                for item in job.items
                if item.state.value == "published" and needle in item.document.lower()
            )
        start = (page - 1) * page_size
        selected = tuple(results[start : start + page_size])
        return selected, start + page_size < len(results)

    async def notifications(self, actor_id: UUID):
        notifications: list[JobEvent] = []
        for team in await self.teams(actor_id):
            jobs, _ = await self.jobs(actor_id, team.id, page=1, page_size=100)
            for job in jobs:
                notifications.extend(
                    event
                    for event in await self._repository.job_events_after(
                        job.id, team.id, 0
                    )
                    if event.event_type
                    in {"proceso.completed", "proceso.failed", "proceso.cancelled"}
                )
        return tuple(
            sorted(notifications, key=lambda event: event.sequence, reverse=True)
        )

    async def submit_text(
        self,
        *,
        actor_id: UUID,
        team_id: UUID,
        credential_version_id: UUID,
        filename: str,
        documents: str,
        sources: tuple[str, ...],
        storage: ObjectStorage,
    ) -> Job:
        """Persist the submitted input through the immutable storage port, then admit it."""
        await self.require_leader(actor_id, team_id)
        content = documents.encode("utf-8")
        reference = ObjectReference(
            id=uuid4(),
            team_id=team_id,
            provider="portal-browser",
            container="submissions",
            object_key=f"{team_id}/{uuid4()}.txt",
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            content_type="text/plain; charset=utf-8",
        )
        await storage.put_immutable(reference, content)
        await self._repository.add_object_reference(reference)
        lines = tuple(
            InputLine(ordinal, value.strip())
            for ordinal, value in enumerate(documents.splitlines(), start=1)
            if value.strip()
        )
        return await self.submit(
            SubmitJob(
                actor_id=actor_id,
                team_id=team_id,
                credential_version_id=credential_version_id,
                input_object_id=reference.id,
                filename=filename.strip() or "documentos.txt",
                sources=sources,
                lines=lines,
            )
        )

    async def credentials(
        self, actor_id: UUID, team_id: UUID
    ) -> tuple[CredentialVersion, ...]:
        await self.require_leader(actor_id, team_id)
        return await self._repository.credentials_for_team(team_id)

    async def create_credential(
        self,
        actor_id: UUID,
        team_id: UUID,
        *,
        label: str,
        provider: str,
        config_ciphertext: bytes,
        key_id: str,
    ) -> CredentialVersion:
        await self.require_leader(actor_id, team_id)
        return await self._repository.create_credential(
            team_id, label, provider, config_ciphertext, key_id, actor_id
        )

    async def create_user(
        self, actor_id: UUID, email: str, password: str
    ) -> PortalUser:
        await self.require_site_admin(actor_id)
        if len(password) < 12:
            msg = "la contraseña debe tener al menos 12 caracteres"
            raise ValueError(msg)
        return await self._repository.create_user(email, hash_password(password))

    async def create_team(
        self, actor_id: UUID, slug: str, name: str, leader_id: UUID
    ) -> Team:
        await self.require_site_admin(actor_id)
        return await self._repository.create_team(slug, name, actor_id, leader_id)

    async def add_member(
        self, actor_id: UUID, team_id: UUID, user_id: UUID, role: TeamRole
    ) -> None:
        await self.require_site_admin(actor_id)
        await self._repository.add_member(team_id, user_id, role)

    async def require_reader(self, actor_id: UUID, team_id: UUID) -> TeamRole:
        role = await self._repository.role_for(actor_id, team_id)
        if role is None:
            raise PermissionDenied("no pertenece al equipo")
        return role

    async def require_leader(self, actor_id: UUID, team_id: UUID) -> TeamRole:
        role = await self.require_reader(actor_id, team_id)
        if role not in {TeamRole.SITE_ADMIN, TeamRole.TEAM_LEADER}:
            raise PermissionDenied("solo un líder del equipo puede gestionar procesos")
        return role

    async def require_site_admin(self, actor_id: UUID) -> PortalUser:
        user = await self._repository.user_by_id(actor_id)
        if user is None or not user.is_site_admin:
            raise PermissionDenied("solo el administrador del sitio puede continuar")
        return user
