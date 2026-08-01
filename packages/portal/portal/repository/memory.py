from __future__ import annotations

import asyncio

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from portal.domain.models import (
    ACTIVE_JOB_STATES,
    MAX_ACTIVE_JOBS,
    BrowserSession,
    CredentialState,
    CredentialVersion,
    DeliveryChannel,
    ItemState,
    Job,
    JobEvent,
    JobItem,
    JobState,
    NotificationIntent,
    PortalUser,
    ProxyProvider,
    SearchResult,
    SubmissionPlan,
    SubmitJob,
    Team,
    TeamRole,
)
from portal.storage.port import ObjectReference


class InMemoryPortalRepository:
    """Concurrency-faithful test adapter for the PostgreSQL portal boundary."""

    def __init__(self) -> None:
        self._gate = asyncio.Lock()
        self._roles: dict[tuple[UUID, UUID], TeamRole] = {}
        self._credentials: dict[UUID, CredentialVersion] = {}
        self._users: dict[UUID, tuple[PortalUser, str]] = {}
        self._sessions: dict[str, tuple[UUID, str, datetime]] = {}
        self._login_csrf: dict[str, datetime] = {}
        self._login_failures: dict[tuple[str, str], list[datetime]] = {}
        self._teams: dict[UUID, Team] = {}
        self._initial_team_id: UUID | None = None
        self._object_references: dict[UUID, ObjectReference] = {}
        self.jobs: dict[UUID, Job] = {}
        self.events: list[JobEvent] = []
        self.outbox: list[NotificationIntent] = []
        self._queue_sequence = 0

    # Test setup helpers intentionally remain synchronous.
    def grant(self, actor_id: UUID, team_id: UUID, role: TeamRole) -> None:
        self._teams.setdefault(
            team_id, Team(team_id, f"equipo-{str(team_id)[:8]}", "Equipo")
        )
        self._roles[actor_id, team_id] = role

    def add_credential(self, credential: CredentialVersion) -> None:
        self._credentials[credential.id] = credential

    def add_user(
        self,
        email: str,
        password_hash: str,
        *,
        is_site_admin: bool = False,
        user_id: UUID | None = None,
    ) -> PortalUser:
        user = PortalUser(user_id or uuid4(), email.lower().strip(), is_site_admin)
        self._users[user.id] = (user, password_hash)
        return user

    async def role_for(self, actor_id: UUID, team_id: UUID) -> TeamRole | None:
        return self._roles.get((actor_id, team_id))

    async def credential(self, credential_version_id: UUID) -> CredentialVersion | None:
        return self._credentials.get(credential_version_id)

    async def admit_submission(self, command: SubmitJob, plan: SubmissionPlan) -> Job:
        async with self._gate:
            self._queue_sequence += 1
            state = self._initial_state(plan)
            job = Job(
                id=uuid4(),
                team_id=command.team_id,
                submitted_by=command.actor_id,
                credential_version_id=command.credential_version_id,
                input_object_id=command.input_object_id,
                filename=command.filename,
                sources=command.sources,
                queue_sequence=self._queue_sequence,
                state=state,
                items=[
                    JobItem(
                        ordinal=item.ordinal, document=item.document, source=item.source
                    )
                    for item in plan.items
                ],
                exclusions=list(plan.exclusions),
                terminal_reason=(
                    "todos_los_registros_excluidos"
                    if state is JobState.COMPLETED
                    else None
                ),
                created_at=datetime.now(UTC),
            )
            self.jobs[job.id] = job
            if state is JobState.COMPLETED:
                self._terminal(job)
            else:
                self._event(job, f"proceso.{state.value}")
            return job

    async def cancel(self, job_id: UUID, team_id: UUID) -> Job | None:
        async with self._gate:
            job = self.jobs.get(job_id)
            if job is None or job.team_id != team_id:
                return None
            if job.state in {JobState.CANCELLED, JobState.COMPLETED, JobState.FAILED}:
                return job
            if job.state is JobState.RUNNING:
                job.state = JobState.CANCELLING
                self._event(job, "proceso.cancelacion_solicitada")
                job.lease_fence += 1
            for item in job.items:
                if item.state in {ItemState.PENDING, ItemState.RUNNING}:
                    item.state = ItemState.CANCELLED
            job.state = JobState.CANCELLED
            self._terminal(job)
            self._promote_fifo()
            return job

    async def record_published_result(
        self, job_id: UUID, item_id: UUID, fence: int, result_object_id: UUID
    ) -> bool:
        async with self._gate:
            job = self.jobs[job_id]
            item = next(item for item in job.items if item.id == item_id)
            if (
                job.state is not JobState.RUNNING
                or job.lease_fence != fence
                or item.state not in {ItemState.PENDING, ItemState.RUNNING}
            ):
                return False
            item.state = ItemState.PUBLISHED
            item.result_object_id = result_object_id
            self._event(job, "proceso.resultado_publicado")
            return True

    async def complete(self, job_id: UUID) -> Job:
        async with self._gate:
            job = self.jobs[job_id]
            if job.state is JobState.RUNNING:
                job.state = JobState.COMPLETED
                self._terminal(job)
                self._promote_fifo()
            return job

    async def search_published(
        self, team_id: UUID, needle: str, *, limit: int, offset: int
    ) -> tuple[tuple[SearchResult, ...], bool]:
        wanted = needle.lower()
        matches = [
            SearchResult(job.id, job.filename, item.document)
            for job in sorted(
                (job for job in self.jobs.values() if job.team_id == team_id),
                key=lambda job: job.queue_sequence,
                reverse=True,
            )
            for item in job.items
            if item.state is ItemState.PUBLISHED and wanted in item.document.lower()
        ]
        page = matches[offset : offset + limit]
        return tuple(page), offset + limit < len(matches)

    async def recent_job_events(
        self, team_ids: tuple[UUID, ...], event_types: tuple[str, ...], *, limit: int
    ) -> tuple[JobEvent, ...]:
        teams = set(team_ids)
        wanted = set(event_types)
        matches = [
            event
            for event in self.events
            if event.event_type in wanted
            and (job := self.jobs.get(event.job_id)) is not None
            and job.team_id in teams
        ]
        matches.sort(key=lambda event: event.sequence, reverse=True)
        return tuple(matches[:limit])

    async def published_jobs(self, team_id: UUID) -> tuple[Job, ...]:
        return tuple(
            job
            for job in self.jobs.values()
            if job.team_id == team_id
            and any(item.state is ItemState.PUBLISHED for item in job.items)
        )

    async def user_by_email(self, email: str) -> tuple[PortalUser, str] | None:
        wanted = email.lower().strip()
        return next(
            (value for value in self._users.values() if value[0].email == wanted), None
        )

    async def user_by_id(self, user_id: UUID) -> PortalUser | None:
        user = self._users.get(user_id)
        return user[0] if user else None

    async def create_user(
        self, email: str, password_hash: str, *, is_site_admin: bool = False
    ) -> PortalUser:
        if await self.user_by_email(email):
            msg = "el correo ya está registrado"
            raise ValueError(msg)
        return self.add_user(email, password_hash, is_site_admin=is_site_admin)

    async def provision_site_admin(self, email: str, password_hash: str) -> PortalUser:
        existing = await self.user_by_email(email)
        if existing:
            user, stored_hash = existing
            if not user.is_site_admin:
                user = PortalUser(user.id, user.email, is_site_admin=True)
                self._users[user.id] = (user, stored_hash)
            return user
        return self.add_user(email, password_hash, is_site_admin=True)

    async def create_session(
        self, user_id: UUID, token_hash: str, csrf_token: str, expires_at: datetime
    ) -> None:
        self._sessions[token_hash] = (user_id, csrf_token, expires_at)

    async def session_user(self, token_hash: str, now: datetime) -> PortalUser | None:
        session = self._sessions.get(token_hash)
        if session is None or session[2] <= now:
            self._sessions.pop(token_hash, None)
            return None
        return await self.user_by_id(session[0])

    async def browser_session(
        self, token_hash: str, now: datetime
    ) -> BrowserSession | None:
        session = self._sessions.get(token_hash)
        if session is None or session[2] <= now:
            self._sessions.pop(token_hash, None)
            return None
        user = await self.user_by_id(session[0])
        return BrowserSession(user, session[1]) if user else None

    async def destroy_session(self, token_hash: str) -> None:
        self._sessions.pop(token_hash, None)

    async def issue_login_csrf(self, token: str, expires_at: datetime) -> None:
        self._login_csrf[token] = expires_at

    async def consume_login_csrf(self, token: str, now: datetime) -> bool:
        expires_at = self._login_csrf.pop(token, None)
        return expires_at is not None and expires_at > now

    async def login_allowed(self, email: str, client_ip: str, now: datetime) -> bool:
        key = (email.lower().strip(), client_ip)
        recent = [
            attempted
            for attempted in self._login_failures.get(key, [])
            if attempted > now - timedelta(minutes=5)
        ]
        self._login_failures[key] = recent
        return len(recent) < 5

    async def record_login_failure(
        self, email: str, client_ip: str, now: datetime
    ) -> None:
        key = (email.lower().strip(), client_ip)
        self._login_failures.setdefault(key, []).append(now)

    async def clear_login_failures(self, email: str, client_ip: str) -> None:
        self._login_failures.pop((email.lower().strip(), client_ip), None)

    async def teams_for_user(self, actor_id: UUID) -> tuple[Team, ...]:
        return tuple(
            Team(team.id, team.slug, team.name, self._roles[actor_id, team.id])
            for team in sorted(self._teams.values(), key=lambda value: value.name)
            if (actor_id, team.id) in self._roles
        )

    async def team(self, team_id: UUID) -> Team | None:
        return self._teams.get(team_id)

    async def team_by_slug(self, slug: str) -> Team | None:
        return next((team for team in self._teams.values() if team.slug == slug), None)

    async def all_teams(self) -> tuple[Team, ...]:
        return tuple(sorted(self._teams.values(), key=lambda value: value.name))

    async def users(self) -> tuple[PortalUser, ...]:
        return tuple(
            user
            for user, _ in sorted(self._users.values(), key=lambda item: item[0].email)
        )

    async def installation_status(self) -> tuple[int, UUID | None]:
        return len(self._teams), self._initial_team_id

    async def create_first_team(self, slug: str, name: str, actor_id: UUID) -> Team:
        async with self._gate:
            if self._teams or self._initial_team_id is not None:
                msg = "la instalación ya tiene un equipo inicial"
                raise ValueError(msg)
            team = self._create_team_locked(slug, name, actor_id, actor_id)
            self._initial_team_id = team.id
            return team

    async def create_team(
        self, slug: str, name: str, created_by: UUID, leader_id: UUID
    ) -> Team:
        async with self._gate:
            return self._create_team_locked(slug, name, created_by, leader_id)

    def _create_team_locked(
        self, slug: str, name: str, created_by: UUID, leader_id: UUID
    ) -> Team:
        if any(team.slug == slug for team in self._teams.values()):
            msg = "el identificador del equipo ya existe"
            raise ValueError(msg)
        if leader_id not in self._users:
            msg = "la persona líder no existe"
            raise ValueError(msg)
        team = Team(uuid4(), slug, name)
        self._teams[team.id] = team
        self._roles[leader_id, team.id] = TeamRole.TEAM_LEADER
        del created_by
        return team

    async def add_member(self, team_id: UUID, user_id: UUID, role: TeamRole) -> None:
        if role not in {TeamRole.TEAM_LEADER, TeamRole.TEAM_MEMBER}:
            msg = "el rol de equipo no es válido"
            raise ValueError(msg)
        async with self._gate:
            if team_id not in self._teams or user_id not in self._users:
                msg = "el equipo o la persona no existe"
                raise ValueError(msg)
            current = self._roles.get((user_id, team_id))
            if (
                current is TeamRole.TEAM_LEADER
                and role is not TeamRole.TEAM_LEADER
                and self._leader_count(team_id) == 1
            ):
                msg = "el equipo debe conservar al menos una persona líder"
                raise ValueError(msg)
            self._roles[user_id, team_id] = role

    async def remove_member(self, team_id: UUID, user_id: UUID) -> None:
        async with self._gate:
            current = self._roles.get((user_id, team_id))
            if current is None:
                return
            if current is TeamRole.TEAM_LEADER and self._leader_count(team_id) == 1:
                msg = "el equipo debe conservar al menos una persona líder"
                raise ValueError(msg)
            del self._roles[user_id, team_id]

    async def members_for_team(
        self, team_id: UUID
    ) -> tuple[tuple[PortalUser, TeamRole], ...]:
        return tuple(
            sorted(
                (
                    (self._users[user_id][0], role)
                    for (member_team_id, user_id), role in self._roles.items()
                    if member_team_id == team_id
                ),
                key=lambda value: value[0].email,
            )
        )

    async def credentials_for_team(
        self, team_id: UUID
    ) -> tuple[CredentialVersion, ...]:
        return tuple(
            credential
            for credential in self._credentials.values()
            if credential.team_id == team_id
        )

    async def start_credential_validation(
        self,
        team_id: UUID,
        label: str,
        provider: str,
        config_ciphertext: bytes,
        key_id: str,
        created_by: UUID,
    ) -> CredentialVersion:
        del config_ciphertext, key_id, created_by
        try:
            selected_provider = ProxyProvider(provider)
        except ValueError as error:
            msg = "el proveedor seleccionado no está disponible"
            raise ValueError(msg) from error
        async with self._gate:
            versions = [
                credential.version
                for credential in self._credentials.values()
                if credential.team_id == team_id and credential.label == label
            ]
            credential = CredentialVersion(
                uuid4(),
                team_id,
                label,
                max(versions, default=0) + 1,
                is_active=False,
                state=CredentialState.VALIDATING,
                provider=selected_provider,
            )
            self.add_credential(credential)
            return credential

    async def finish_credential_validation(
        self,
        credential_version_id: UUID,
        *,
        state: CredentialState,
        detail: str | None,
        actor_id: UUID,
    ) -> CredentialVersion:
        del detail, actor_id
        if state not in {CredentialState.ACTIVE, CredentialState.FAILED}:
            msg = "el estado final de la credencial no es válido"
            raise ValueError(msg)
        async with self._gate:
            credential = self._credentials.get(credential_version_id)
            if credential is None or credential.state is not CredentialState.VALIDATING:
                msg = "la credencial no está pendiente de validación"
                raise ValueError(msg)
            if state is CredentialState.ACTIVE:
                for version_id, current in tuple(self._credentials.items()):
                    if (
                        current.team_id == credential.team_id
                        and current.label == credential.label
                        and current.is_active
                    ):
                        self._credentials[version_id] = CredentialVersion(
                            current.id,
                            current.team_id,
                            current.label,
                            current.version,
                            is_active=False,
                            state=CredentialState.RETIRED,
                            provider=current.provider,
                        )
            completed = CredentialVersion(
                credential.id,
                credential.team_id,
                credential.label,
                credential.version,
                is_active=state is CredentialState.ACTIVE,
                state=state,
                provider=credential.provider,
            )
            self._credentials[credential.id] = completed
            return completed

    async def add_object_reference(self, reference: ObjectReference) -> None:
        self._object_references[reference.id] = reference

    async def jobs_for_team(
        self, team_id: UUID, *, page: int, page_size: int
    ) -> tuple[tuple[Job, ...], int]:
        jobs = sorted(
            (job for job in self.jobs.values() if job.team_id == team_id),
            key=lambda job: job.queue_sequence,
            reverse=True,
        )
        start = (page - 1) * page_size
        return tuple(jobs[start : start + page_size]), len(jobs)

    async def job(self, job_id: UUID, team_id: UUID) -> Job | None:
        job = self.jobs.get(job_id)
        return job if job and job.team_id == team_id else None

    async def job_events_after(
        self, job_id: UUID, team_id: UUID, sequence: int
    ) -> tuple[JobEvent, ...]:
        job = await self.job(job_id, team_id)
        if job is None:
            return ()
        return tuple(
            event
            for event in self.events
            if event.job_id == job_id and event.sequence > sequence
        )

    def _initial_state(self, plan: SubmissionPlan) -> JobState:
        if not plan.items:
            return JobState.COMPLETED
        active = sum(job.state in ACTIVE_JOB_STATES for job in self.jobs.values())
        return JobState.RUNNING if active < MAX_ACTIVE_JOBS else JobState.QUEUED

    def _promote_fifo(self) -> None:
        active = sum(job.state in ACTIVE_JOB_STATES for job in self.jobs.values())
        slots = MAX_ACTIVE_JOBS - active
        queued = sorted(
            (job for job in self.jobs.values() if job.state is JobState.QUEUED),
            key=lambda job: job.queue_sequence,
        )
        for job in queued[:slots]:
            job.state = JobState.RUNNING
            self._event(job, "proceso.running")

    def _event(self, job: Job, event_type: str) -> JobEvent:
        event = JobEvent(
            id=uuid4(),
            job_id=job.id,
            event_type=event_type,
            sequence=len(self.events) + 1,
            created_at=datetime.now(UTC),
        )
        self.events.append(event)
        return event

    def _terminal(self, job: Job) -> None:
        event = self._event(job, f"proceso.{job.state.value}")
        self.outbox.extend(
            NotificationIntent(uuid4(), event.id, channel, job.team_id)
            for channel in DeliveryChannel
        )

    def _leader_count(self, team_id: UUID) -> int:
        return sum(
            role is TeamRole.TEAM_LEADER
            for (member_team_id, _), role in self._roles.items()
            if member_team_id == team_id
        )
