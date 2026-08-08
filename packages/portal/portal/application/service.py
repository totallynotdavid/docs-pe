from __future__ import annotations

import hashlib

from typing import TYPE_CHECKING
from uuid import uuid4

from portal.application.access import (
    AuthorizedService,
    public,
    site_admin,
    site_admin_or_leader,
    site_admin_or_reader,
    team_leader,
    team_reader,
)
from portal.domain.errors import (
    CredentialConfigurationError,
    NotFound,
    PermissionDenied,
    Reason,
)
from portal.domain.models import (
    TERMINAL_JOB_EVENTS,
    CredentialState,
    InputLine,
    SearchResult,
    SubmitJob,
    SystemHealth,
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
        SearchLogEntry,
        TeamSearchActivity,
    )
    from portal.repository.credentials import PostgresCredentialRepository
    from portal.repository.jobs import PostgresJobRepository
    from portal.repository.search_log import PostgresSearchLogRepository
    from portal.repository.teams import PostgresTeamRepository
    from portal.repository.workers import PostgresWorkerRegistry
    from portal.storage.port import ObjectStorage


class PortalService(AuthorizedService):
    """Team-scoped reads and writes. Authentication lives in LoginService."""

    def __init__(
        self,
        teams: PostgresTeamRepository,
        credentials: PostgresCredentialRepository,
        jobs: PostgresJobRepository,
        search_log: PostgresSearchLogRepository,
        workers: PostgresWorkerRegistry,
    ) -> None:
        self._teams = teams
        self._credentials = credentials
        self._jobs = jobs
        self._search_log = search_log
        self._workers = workers

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

    @site_admin_or_reader()
    async def team(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> Team:
        role = await self._teams.role_for(actor_id, team_id)
        team = await self._teams.team(team_id)

        if team is None:
            raise NotFound(Reason.TEAM_NOT_FOUND)

        return Team(team.id, team.slug, team.name, role)

    @site_admin_or_reader()
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

        results, has_more = await self._jobs.search_published(
            team_id,
            needle,
            limit=page_size,
            offset=(page - 1) * page_size,
        )

        # Logged on every page, not just the first: page 2 of a scan is still
        # a search someone ran, and a leader scanning activity wants the
        # real count of lookups, not just distinct queries.
        await self._search_log.record(team_id, actor_id, needle, len(results))

        return results, has_more

    @site_admin_or_leader()
    async def recent_searches(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> tuple[SearchLogEntry, ...]:
        return await self._search_log.recent_for_team(team_id)

    @site_admin
    async def team_search_activity(
        self, actor_id: UUID
    ) -> tuple[TeamSearchActivity, ...]:
        return await self._search_log.team_totals()

    @site_admin
    async def system_health(self, actor_id: UUID) -> SystemHealth:
        return SystemHealth(
            queue=await self._jobs.queue_health(),
            workers=await self._workers.all_workers_with_status(),
        )

    @public
    async def notifications(
        self,
        actor_id: UUID,
    ) -> tuple[JobEvent, ...]:
        # A site admin isn't necessarily a member of any team, so their own
        # memberships would leave this feed empty even though they're meant
        # to see activity across the whole installation.
        if await self._teams.is_site_admin(actor_id):
            team_ids = tuple(team.id for team in await self._teams.all_teams())
        else:
            team_ids = tuple(team.id for team in await self.teams(actor_id))

        return await self._jobs.recent_job_events(
            team_ids,
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

    @site_admin_or_leader()
    async def credentials(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> tuple[CredentialVersion, ...]:
        return await self._credentials.credentials_for_team(team_id)

    @site_admin_or_leader()
    async def rename_credential(
        self,
        actor_id: UUID,
        team_id: UUID,
        credential_id: UUID,
        label: str,
    ) -> None:
        clean = label.strip()

        if not 1 <= len(clean) <= 120:
            raise CredentialConfigurationError(Reason.LABEL_LENGTH)

        await self._credentials.rename_credential(credential_id, team_id, clean)

    async def _require_site_admin(self, actor_id: UUID) -> None:
        if not await self._teams.is_site_admin(actor_id):
            raise PermissionDenied(Reason.SITE_ADMIN_REQUIRED)

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

    async def _require_reader_or_site_admin(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> None:
        if await self._teams.is_site_admin(actor_id):
            return

        await self._require_reader(actor_id, team_id)

    async def _require_leader_or_site_admin(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> None:
        if await self._teams.is_site_admin(actor_id):
            return

        await self._require_leader(actor_id, team_id)

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
