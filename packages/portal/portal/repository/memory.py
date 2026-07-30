from __future__ import annotations

import asyncio

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from portal.domain.models import (
    ACTIVE_JOB_STATES,
    MAX_ACTIVE_JOBS,
    BrowserSession,
    CredentialVersion,
    DeliveryChannel,
    ItemState,
    Job,
    JobEvent,
    JobItem,
    JobState,
    NotificationIntent,
    PortalUser,
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
        self._site_admins: set[UUID] = set()
        self._credentials: dict[UUID, CredentialVersion] = {}
        self._users: dict[UUID, tuple[PortalUser, str]] = {}
        self._sessions: dict[str, tuple[UUID, str, datetime]] = {}
        self._login_csrf: dict[str, datetime] = {}
        self._login_failures: dict[tuple[str, str], list[datetime]] = {}
        self._teams: dict[UUID, Team] = {}
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
        if role is TeamRole.SITE_ADMIN:
            self._site_admins.add(actor_id)
            user = self._users.get(actor_id)
            if user is not None:
                self._users[actor_id] = (
                    PortalUser(actor_id, user[0].email, is_site_admin=True),
                    user[1],
                )
            return
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
        if is_site_admin:
            self._site_admins.add(user.id)
        return user

    async def role_for(self, actor_id: UUID, team_id: UUID) -> TeamRole | None:
        if actor_id in self._site_admins:
            return TeamRole.SITE_ADMIN
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

    async def bootstrap_site_admin(self, email: str, password_hash: str) -> PortalUser:
        existing = await self.user_by_email(email)
        if existing:
            return existing[0]
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
        if actor_id in self._site_admins:
            return tuple(
                Team(team.id, team.slug, team.name, TeamRole.SITE_ADMIN)
                for team in sorted(self._teams.values(), key=lambda value: value.name)
            )
        return tuple(
            Team(team.id, team.slug, team.name, self._roles[actor_id, team.id])
            for team in sorted(self._teams.values(), key=lambda value: value.name)
            if (actor_id, team.id) in self._roles
        )

    async def team(self, team_id: UUID) -> Team | None:
        return self._teams.get(team_id)

    async def create_team(
        self, slug: str, name: str, created_by: UUID, leader_id: UUID
    ) -> Team:
        if any(team.slug == slug for team in self._teams.values()):
            msg = "el identificador del equipo ya existe"
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
        self._roles[user_id, team_id] = role

    async def credentials_for_team(
        self, team_id: UUID
    ) -> tuple[CredentialVersion, ...]:
        return tuple(
            credential
            for credential in self._credentials.values()
            if credential.team_id == team_id
        )

    async def create_credential(
        self,
        team_id: UUID,
        label: str,
        provider: str,
        config_ciphertext: bytes,
        key_id: str,
        created_by: UUID,
    ) -> CredentialVersion:
        del provider, config_ciphertext, key_id, created_by
        versions = [
            credential.version
            for credential in self._credentials.values()
            if credential.team_id == team_id and credential.label == label
        ]
        credential = CredentialVersion(
            uuid4(), team_id, label, max(versions, default=0) + 1
        )
        self.add_credential(credential)
        return credential

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
