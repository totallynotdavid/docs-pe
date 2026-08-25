from __future__ import annotations

import hashlib

from typing import TYPE_CHECKING
from uuid import uuid4

from portal.application.access import (
    AuthorizedService,
    public,
    site_admin,
    site_admin_or_global_search,
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
    SubmitJob,
    SystemHealth,
    Team,
    TeamRole,
)
from portal.domain.planning import SOURCE_FRESHNESS, build_review, plan_submission
from portal.storage.port import ObjectReference


if TYPE_CHECKING:
    from uuid import UUID

    from portal.domain.models import (
        CredentialVersion,
        Entry,
        Job,
        JobEvent,
        JobItem,
        JobItemCounts,
        SearchLogEntry,
        SubmissionReview,
        TeamSearchActivity,
    )
    from portal.repository.credentials import PostgresCredentialRepository
    from portal.repository.entries import PostgresEntryRepository
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
        entries: PostgresEntryRepository,
        search_log: PostgresSearchLogRepository,
        workers: PostgresWorkerRegistry,
    ) -> None:
        self._teams = teams
        self._credentials = credentials
        self._jobs = jobs
        self._entries = entries
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

        return Team(
            team.id,
            team.slug,
            team.name,
            role,
            has_global_search=team.has_global_search,
        )

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
    async def job_items(
        self,
        actor_id: UUID,
        team_id: UUID,
        job_id: UUID,
        *,
        page: int,
        page_size: int = 20,
    ) -> tuple[tuple[JobItem, ...], int]:
        return await self._jobs.items_for_job(
            job_id,
            team_id,
            page=page,
            page_size=page_size,
        )

    @team_reader()
    async def job_progress_counts(
        self,
        actor_id: UUID,
        team_id: UUID,
        job_id: UUID,
    ) -> JobItemCounts:
        return await self._jobs.item_counts(job_id, team_id)

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
    ) -> tuple[tuple[Entry, ...], bool]:
        needle = query.strip()

        if not needle:
            return (), False

        results, has_more = await self._entries.search_team(
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

    @site_admin_or_global_search()
    async def global_search(
        self,
        actor_id: UUID,
        query: str,
        *,
        page: int,
        page_size: int = 20,
    ) -> tuple[tuple[Entry, ...], bool]:
        """Search across every team's collected entries, not just the
        caller's own. Never logged to portal_search_log: that table is
        team-scoped activity for a team's own leaders and doesn't have a
        row shape for a cross-team lookup."""
        needle = query.strip()

        if not needle:
            return (), False

        return await self._entries.search_global(
            needle,
            limit=page_size,
            offset=(page - 1) * page_size,
        )

    @team_reader()
    async def entry(self, actor_id: UUID, team_id: UUID, entry_id: UUID) -> Entry:
        found = await self._entries.entry_for_team(team_id, entry_id)

        if found is None:
            raise NotFound(Reason.ENTRY_NOT_FOUND)

        return found

    @site_admin_or_global_search()
    async def global_entry(self, actor_id: UUID, entry_id: UUID) -> Entry:
        found = await self._entries.entry_by_id(entry_id)

        if found is None:
            raise NotFound(Reason.ENTRY_NOT_FOUND)

        return found

    @team_leader()
    async def preview_submission(
        self,
        actor_id: UUID,
        team_id: UUID,
        lines: tuple[InputLine, ...],
        sources: tuple[str, ...],
    ) -> SubmissionReview:
        """What a submission would do, before any job exists: which lines
        are valid, which are excluded, and which this team already has a
        fresh answer for. The leader chooses reuse or a full rescan from
        this before anything is created -- see confirm_submission."""
        plan = plan_submission(lines, sources)

        if not plan.items:
            return build_review(plan, frozenset())

        pairs = frozenset((item.document, item.source) for item in plan.items)
        reusable = await self._entries.reusable_for_team(
            team_id,
            pairs,
            freshness=SOURCE_FRESHNESS,
        )

        return build_review(plan, frozenset(reusable.keys()))

    @team_leader()
    async def input_reference(
        self,
        actor_id: UUID,
        team_id: UUID,
        reference_id: UUID,
    ) -> ObjectReference:
        reference = await self._jobs.object_reference(reference_id, team_id)

        if reference is None:
            raise NotFound(Reason.INPUT_NOT_FOUND)

        return reference

    @team_leader()
    async def confirm_submission(
        self,
        *,
        actor_id: UUID,
        team_id: UUID,
        credential_version_id: UUID,
        filename: str,
        input_object_id: UUID,
        lines: tuple[InputLine, ...],
        sources: tuple[str, ...],
        reuse: bool,
    ) -> Job:
        """Create the job after the leader has seen preview_submission's
        review (or immediately, when that review had nothing to show:
        see routes/jobs.py). The upload itself already happened -- this
        never touches storage."""
        await self._require_active_credential(credential_version_id, team_id)

        return await self._admit(
            SubmitJob(
                actor_id=actor_id,
                team_id=team_id,
                credential_version_id=credential_version_id,
                input_object_id=input_object_id,
                filename=filename,
                sources=sources,
                lines=lines,
                reuse=reuse,
            )
        )

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
    async def set_global_search(
        self,
        actor_id: UUID,
        team_id: UUID,
        *,
        enabled: bool,
    ) -> None:
        del actor_id
        await self._teams.set_global_search(team_id, enabled=enabled)

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
    async def store_upload(
        self,
        actor_id: UUID,
        team_id: UUID,
        *,
        content: bytes,
        content_type: str,
        storage: ObjectStorage,
    ) -> ObjectReference:
        """Store an uploaded CSV and record it, ahead of knowing whether the
        submission will proceed as-is or go through a reuse review first
        (see routes/jobs.py). Storing early is harmless even if the leader
        never confirms: an orphaned object costs disk, nothing else."""
        del actor_id

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

        return reference

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

    async def _require_global_search(self, actor_id: UUID) -> None:
        if await self._teams.is_site_admin(actor_id):
            return

        if await self._teams.any_team_has_global_search(actor_id):
            return

        raise PermissionDenied(Reason.GLOBAL_SEARCH_REQUIRED)

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
        plan = plan_submission(command.lines, command.sources)
        reusable: dict[tuple[str, str], UUID] = {}

        if command.reuse and plan.items:
            pairs = frozenset((item.document, item.source) for item in plan.items)
            reusable = await self._entries.reusable_for_team(
                command.team_id,
                pairs,
                freshness=SOURCE_FRESHNESS,
            )

        return await self._jobs.admit_submission(command, plan, reusable)
