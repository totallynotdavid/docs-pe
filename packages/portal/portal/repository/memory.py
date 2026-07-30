from __future__ import annotations

import asyncio

from uuid import UUID, uuid4

from portal.domain.models import (
    ACTIVE_JOB_STATES,
    MAX_ACTIVE_JOBS,
    CredentialVersion,
    DeliveryChannel,
    ItemState,
    Job,
    JobEvent,
    JobItem,
    JobState,
    NotificationIntent,
    SubmissionPlan,
    SubmitJob,
    TeamRole,
)


class InMemoryPortalRepository:
    """Concurrency-faithful test double for the PostgreSQL queue gate.

    Its lock mirrors the one-row ``queue_control ... FOR UPDATE`` transaction in the
    production adapter. It is deliberately test-only; no process-local queue exists
    in the deployed architecture.
    """

    def __init__(self) -> None:
        self._gate = asyncio.Lock()
        self._roles: dict[tuple[UUID, UUID], TeamRole] = {}
        self._site_admins: set[UUID] = set()
        self._credentials: dict[UUID, CredentialVersion] = {}
        self.jobs: dict[UUID, Job] = {}
        self.events: list[JobEvent] = []
        self.outbox: list[NotificationIntent] = []
        self._queue_sequence = 0

    def grant(self, actor_id: UUID, team_id: UUID, role: TeamRole) -> None:
        if role is TeamRole.SITE_ADMIN:
            self._site_admins.add(actor_id)
            return
        self._roles[actor_id, team_id] = role

    def add_credential(self, credential: CredentialVersion) -> None:
        self._credentials[credential.id] = credential

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
                        ordinal=item.ordinal,
                        document=item.document,
                        source=item.source,
                    )
                    for item in plan.items
                ],
                exclusions=list(plan.exclusions),
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
        """Apply a worker result only if the cancellation/lease fence still matches."""
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
        event = JobEvent(id=uuid4(), job_id=job.id, event_type=event_type)
        self.events.append(event)
        return event

    def _terminal(self, job: Job) -> None:
        event = self._event(job, f"proceso.{job.state.value}")
        self.outbox.extend(
            NotificationIntent(uuid4(), event.id, channel, job.team_id)
            for channel in DeliveryChannel
        )
