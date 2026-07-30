from __future__ import annotations

from typing import Protocol
from uuid import UUID

from portal.domain.models import (
    ClaimedWork,
    CredentialVersion,
    Job,
    SubmissionPlan,
    SubmitJob,
    TeamRole,
)


class PortalRepository(Protocol):
    """Transactional port; production implementations must use PostgreSQL only."""

    async def role_for(self, actor_id: UUID, team_id: UUID) -> TeamRole | None: ...

    async def credential(
        self, credential_version_id: UUID
    ) -> CredentialVersion | None: ...

    async def admit_submission(
        self, command: SubmitJob, plan: SubmissionPlan
    ) -> Job: ...

    async def cancel(self, job_id: UUID, team_id: UUID) -> Job | None: ...

    async def published_jobs(self, team_id: UUID) -> tuple[Job, ...]: ...


class WorkerQueue(Protocol):
    """PostgreSQL item lease and result-write protocol used by worker processes."""

    async def claim(
        self, worker_id: str, sources: tuple[str, ...]
    ) -> ClaimedWork | None: ...

    async def publish(
        self, item_id: UUID, worker_id: str, fence: int, result_object_id: UUID
    ) -> bool: ...
