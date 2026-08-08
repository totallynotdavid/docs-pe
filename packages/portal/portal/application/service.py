from __future__ import annotations

import hashlib

from typing import TYPE_CHECKING
from uuid import uuid4

from portal.application.access import (
    AuthorizedService,
    public,
    team_leader,
    team_reader,
)
from portal.domain.errors import NotFound, PermissionDenied, Reason
from portal.domain.models import (
    TERMINAL_JOB_EVENTS,
    CredentialState,
    InputLine,
    SearchResult,
    SubmitJob,
    Team,
    TeamRole,
)
from portal.domain.planning import plan_submission
from portal.storage.port import ObjectReference


if TYPE_CHECKING:
    from uuid import UUID

    from portal.domain.models import (
        CredentialVersion,
        Job,
        JobEvent,
    )
    from portal.repository.credentials import PostgresCredentialRepository
    from portal.repository.jobs import PostgresJobRepository
    from portal.repository.teams import PostgresTeamRepository
    from portal.storage.port import ObjectStorage


class PortalService(AuthorizedService):
    """Team-scoped reads and writes. Authentication lives in LoginService."""

    def __init__(
        self,
        teams: PostgresTeamRepository,
        credentials: PostgresCredentialRepository,
        jobs: PostgresJobRepository,
    ) -> None:
        self._teams = teams
        self._credentials = credentials
        self._jobs = jobs

    @team_leader(
        actor_id=lambda a: a["command"].actor_id,
        team_id=lambda a: a["command"].team_id,
    )
    async def submit(self, command: SubmitJob) -> Job:
        await self._require_active_credential(
            command.credential_version_id,
            command.team_id,
        )

        return await self._admit(command)

    @team_leader()
    async def cancel(
        self,
        actor_id: UUID,
        team_id: UUID,
        job_id: UUID,
    ) -> Job:
        job = await self._jobs.cancel(job_id, team_id)

        if job is None:
            raise NotFound(Reason.JOB_NOT_FOUND)

        return job

    @team_reader()
    async def published_results(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> tuple[Job, ...]:
        return await self._jobs.published_jobs(team_id)

    @public
    async def teams(self, actor_id: UUID) -> tuple[Team, ...]:
        return await self._teams.teams_for_user(actor_id)

    @team_reader()
    async def team(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> Team:
        role = await self._require_reader(actor_id, team_id)
        team = await self._teams.team(team_id)

        if team is None:
            raise NotFound(Reason.TEAM_NOT_FOUND)

        return Team(team.id, team.slug, team.name, role)

    @team_reader()
    async def jobs(
        self,
        actor_id: UUID,
        team_id: UUID,
        *,
        page: int,
        page_size: int = 20,
    ) -> tuple[tuple[Job, ...], int]:
        return await self._jobs.jobs_for_team(
            team_id,
            page=page,
            page_size=page_size,
        )

    @team_reader()
    async def job(
        self,
        actor_id: UUID,
        team_id: UUID,
        job_id: UUID,
    ) -> Job:
        job = await self._jobs.job(job_id, team_id)

        if job is None:
            raise NotFound(Reason.JOB_NOT_FOUND)

        return job

    @team_reader()
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

    @team_reader()
    async def search(
        self,
        actor_id: UUID,
        team_id: UUID,
        query: str,
        *,
        page: int,
        page_size: int = 20,
    ) -> tuple[tuple[SearchResult, ...], bool]:
        needle = query.strip()

        if not needle:
            return (), False

        return await self._jobs.search_published(
            team_id,
            needle,
            limit=page_size,
            offset=(page - 1) * page_size,
        )

    @public
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

    @team_leader()
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

    @team_leader()
    async def credentials(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> tuple[CredentialVersion, ...]:
        return await self._credentials.credentials_for_team(team_id)

    async def _require_reader(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> TeamRole:
        role = await self._teams.role_for(actor_id, team_id)

        if role is None:
            raise PermissionDenied(Reason.NOT_A_MEMBER)

        return role

    async def _require_leader(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> TeamRole:
        role = await self._require_reader(actor_id, team_id)

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
