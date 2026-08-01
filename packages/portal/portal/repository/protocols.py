from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from portal.domain.models import (
    BrowserSession,
    ClaimedWork,
    CredentialState,
    CredentialVersion,
    Job,
    JobEvent,
    PortalUser,
    SearchResult,
    SubmissionPlan,
    SubmitJob,
    Team,
    TeamRole,
)
from portal.storage.port import ObjectReference


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

    async def search_published(
        self, team_id: UUID, needle: str, *, limit: int, offset: int
    ) -> tuple[tuple[SearchResult, ...], bool]: ...

    async def recent_job_events(
        self, team_ids: tuple[UUID, ...], event_types: tuple[str, ...], *, limit: int
    ) -> tuple[JobEvent, ...]: ...

    # Browser identity and administrative control plane. Keeping these on the
    # transactional repository ensures HTTP routes never make authorization
    # decisions from client-controlled values.
    async def user_by_email(self, email: str) -> tuple[PortalUser, str] | None: ...

    async def user_by_id(self, user_id: UUID) -> PortalUser | None: ...

    async def create_user(
        self, email: str, password_hash: str, *, is_site_admin: bool = False
    ) -> PortalUser: ...

    async def provision_site_admin(
        self, email: str, password_hash: str
    ) -> PortalUser: ...

    async def create_session(
        self, user_id: UUID, token_hash: str, csrf_token: str, expires_at: datetime
    ) -> None: ...

    async def session_user(
        self, token_hash: str, now: datetime
    ) -> PortalUser | None: ...

    async def browser_session(
        self, token_hash: str, now: datetime
    ) -> BrowserSession | None: ...

    async def destroy_session(self, token_hash: str) -> None: ...

    async def issue_login_csrf(self, token: str, expires_at: datetime) -> None: ...

    async def consume_login_csrf(self, token: str, now: datetime) -> bool: ...

    async def login_allowed(
        self, email: str, client_ip: str, now: datetime
    ) -> bool: ...

    async def record_login_failure(
        self, email: str, client_ip: str, now: datetime
    ) -> None: ...

    async def clear_login_failures(self, email: str, client_ip: str) -> None: ...

    async def teams_for_user(self, actor_id: UUID) -> tuple[Team, ...]: ...

    async def users(self) -> tuple[PortalUser, ...]: ...

    async def team(self, team_id: UUID) -> Team | None: ...

    async def team_by_slug(self, slug: str) -> Team | None: ...

    async def all_teams(self) -> tuple[Team, ...]: ...

    async def installation_status(self) -> tuple[int, UUID | None]: ...

    async def create_first_team(self, slug: str, name: str, actor_id: UUID) -> Team: ...

    async def create_team(
        self, slug: str, name: str, created_by: UUID, leader_id: UUID
    ) -> Team: ...

    async def add_member(
        self, team_id: UUID, user_id: UUID, role: TeamRole
    ) -> None: ...

    async def remove_member(self, team_id: UUID, user_id: UUID) -> None: ...

    async def members_for_team(
        self, team_id: UUID
    ) -> tuple[tuple[PortalUser, TeamRole], ...]: ...

    async def credentials_for_team(
        self, team_id: UUID
    ) -> tuple[CredentialVersion, ...]: ...

    async def start_credential_validation(
        self,
        team_id: UUID,
        label: str,
        provider: str,
        config_ciphertext: bytes,
        key_id: str,
        created_by: UUID,
    ) -> CredentialVersion: ...

    async def finish_credential_validation(
        self,
        credential_version_id: UUID,
        *,
        state: CredentialState,
        detail: str | None,
        actor_id: UUID,
    ) -> CredentialVersion: ...

    async def add_object_reference(self, reference: ObjectReference) -> None: ...

    async def jobs_for_team(
        self, team_id: UUID, *, page: int, page_size: int
    ) -> tuple[tuple[Job, ...], int]: ...

    async def job(self, job_id: UUID, team_id: UUID) -> Job | None: ...

    async def job_events_after(
        self, job_id: UUID, team_id: UUID, sequence: int
    ) -> tuple[JobEvent, ...]: ...


class WorkerQueue(Protocol):
    """PostgreSQL item lease and result-write protocol used by worker processes."""

    async def claim(
        self, worker_id: str, sources: tuple[str, ...]
    ) -> ClaimedWork | None: ...

    async def publish(
        self, item_id: UUID, worker_id: str, fence: int, result_object_id: UUID
    ) -> bool: ...
