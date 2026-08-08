from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4


class TeamRole(StrEnum):
    TEAM_LEADER = "team_leader"
    TEAM_MEMBER = "team_member"


class CredentialState(StrEnum):
    DRAFT = "draft"
    VALIDATING = "validating"
    ACTIVE = "active"
    FAILED = "failed"
    RETIRED = "retired"


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ItemState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PUBLISHED = "published"
    EXCLUDED = "excluded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class DeliveryChannel(StrEnum):
    IN_APP = "in_app"
    EMAIL = "email"
    KAPSO_WHATSAPP = "kapso_whatsapp"


ACTIVE_JOB_STATES = frozenset({JobState.RUNNING, JobState.CANCELLING})
TERMINAL_JOB_STATES = frozenset(
    {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
)
PUBLISHED_ITEM_STATES = frozenset({ItemState.PUBLISHED})

TERMINAL_JOB_EVENTS = (
    "job.completed",
    "job.failed",
    "job.cancelled",
)

MAX_ACTIVE_JOBS = 5

# Bounds worker lease retries, not lookup retries.
MAX_LEASE_ATTEMPTS = 4


class AuditAction(StrEnum):
    LOGIN_SUCCEEDED = "login.succeeded"
    LOGIN_FAILED = "login.failed"
    SESSION_DESTROYED = "session.destroyed"
    PERMISSION_DENIED = "permission.denied"

    MFA_ENROLLED = "mfa.enrolled"
    MFA_REMOVED = "mfa.removed"
    PASSKEY_REGISTERED = "passkey.registered"
    PASSKEY_REMOVED = "passkey.removed"
    STEP_UP_VERIFIED = "auth.step_up_verified"
    STEP_UP_FAILED = "auth.step_up_failed"

    USER_CREATED = "admin.user_created"
    USER_DEACTIVATED = "admin.user_deactivated"
    USER_REACTIVATED = "admin.user_reactivated"
    USER_DELETED = "admin.user_deleted"
    USER_PROMOTED = "admin.user_promoted"
    USER_DEMOTED = "admin.user_demoted"
    USER_PASSWORD_RESET = "admin.user_password_reset"
    TEAM_CREATED = "admin.team_created"
    MEMBER_ADDED = "team.member_added"
    MEMBER_REMOVED = "team.member_removed"

    CREDENTIAL_CONFIGURED = "credential.configured"
    CREDENTIAL_REVEALED = "credential.revealed"

    WORKER_ISSUED = "worker.issued"
    WORKER_REVOKED = "worker.revoked"


class LoginRejection(StrEnum):
    """Why a login stopped. Recorded, never shown: the page stays generic."""

    HUMAN_CHECK = "human_check"
    CSRF = "csrf"
    THROTTLED = "throttled"
    CREDENTIALS = "credentials"
    MFA_EXPIRED = "mfa_expired"
    MFA_CODE = "mfa_code"
    PASSKEY_INVALID = "passkey_invalid"


@dataclass(frozen=True)
class RequestTrace:
    """How the edge saw a request: the client address and Cloudflare's ray id.

    `ip` is None when no trustworthy address was available, which keeps the
    audit log's inet column honest instead of storing a placeholder.
    """

    ip: str | None = None
    ray_id: str | None = None

    @property
    def source(self) -> str:
        """Rate-limit bucket. Unattributable requests share one bucket."""
        return self.ip or "unknown"


@dataclass(frozen=True)
class AuditEvent:
    action: AuditAction
    actor_id: UUID | None = None
    target_type: str | None = None
    target_id: UUID | None = None
    trace: RequestTrace | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PortalUser:
    id: UUID
    email: str
    is_site_admin: bool = False
    mfa_enabled: bool = False
    has_passkey: bool = False
    is_active: bool = True

    # Promotion is waiting on this user completing their own enrollment: see
    # ProvisioningService.promote_to_site_admin.
    pending_site_admin: bool = False

    @property
    def has_second_factor(self) -> bool:
        return self.mfa_enabled or self.has_passkey


@dataclass(frozen=True)
class BrowserSession:
    user: PortalUser
    csrf_token: str
    mfa_verified_at: datetime | None = None


@dataclass(frozen=True)
class WebAuthnCredential:
    id: UUID
    user_id: UUID
    credential_id: bytes
    public_key: bytes
    sign_count: int
    transports: tuple[str, ...]
    label: str
    created_at: datetime
    last_used_at: datetime | None = None


@dataclass(frozen=True)
class WorkerIdentity:
    id: UUID
    worker_id: str
    tailscale_hostname: str


@dataclass(frozen=True)
class Team:
    id: UUID
    slug: str
    name: str
    role: TeamRole | None = None


@dataclass(frozen=True)
class CredentialVersion:
    id: UUID
    team_id: UUID
    credential_id: UUID
    label: str
    version: int
    is_active: bool = True
    state: CredentialState = CredentialState.ACTIVE
    provider: str = ""


@dataclass(frozen=True)
class ProtectedSecret:
    """An enveloped payload: what gets stored, and all that gets stored.

    The data key that encrypted `ciphertext` exists only in wrapped form here.
    `master_key_version` names the keyring entry that can unwrap it, which is
    what lets a rotation leave older rows readable until they are re-wrapped.
    """

    ciphertext: bytes
    wrapped_data_key: bytes
    master_key_version: str


@dataclass(frozen=True)
class JobCredential:
    """The proxy credential a job's items must be fetched through.

    Stays enveloped until the boundary that hands work to a worker, so the
    repository never holds plaintext proxy passwords.
    """

    provider: str
    config: ProtectedSecret


@dataclass(frozen=True)
class InputLine:
    ordinal: int
    value: str


@dataclass(frozen=True)
class PlannedItem:
    ordinal: int
    document: str
    source: str


@dataclass(frozen=True)
class ExcludedInput:
    ordinal: int
    value: str
    reason: str


@dataclass(frozen=True)
class SubmissionPlan:
    items: tuple[PlannedItem, ...]
    exclusions: tuple[ExcludedInput, ...]


@dataclass(frozen=True)
class SubmitJob:
    actor_id: UUID
    team_id: UUID
    credential_version_id: UUID
    input_object_id: UUID
    filename: str
    sources: tuple[str, ...]
    lines: tuple[InputLine, ...]


@dataclass
class JobItem:
    id: UUID = field(default_factory=uuid4)
    ordinal: int = 0
    document: str = ""
    source: str = ""
    state: ItemState = ItemState.PENDING
    lease_fence: int = 0
    result_object_id: UUID | None = None


@dataclass
class Job:
    id: UUID
    team_id: UUID
    submitted_by: UUID
    credential_version_id: UUID
    input_object_id: UUID
    filename: str
    sources: tuple[str, ...]
    queue_sequence: int
    state: JobState
    lease_fence: int = 0
    items: list[JobItem] = field(default_factory=list)
    exclusions: list[ExcludedInput] = field(default_factory=list)
    terminal_reason: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class ClaimedWork:
    item_id: UUID
    job_id: UUID
    source: str
    document: str
    lease_fence: int


@dataclass(frozen=True)
class JobEvent:
    id: UUID
    job_id: UUID
    event_type: str
    sequence: int = 0
    created_at: datetime | None = None


@dataclass(frozen=True)
class NotificationIntent:
    id: UUID
    event_id: UUID
    channel: DeliveryChannel
    team_id: UUID


@dataclass(frozen=True)
class SearchResult:
    job_id: UUID
    filename: str
    document: str


@dataclass(frozen=True)
class SearchLogEntry:
    id: UUID
    query: str
    result_count: int
    actor_email: str
    created_at: datetime


@dataclass(frozen=True)
class TeamSearchActivity:
    team_id: UUID
    team_name: str
    search_count: int
    last_searched_at: datetime | None


@dataclass(frozen=True)
class WorkerStatus:
    worker_id: str
    tailscale_hostname: str
    online: bool
    last_seen_at: datetime | None
    cpu_percent: float | None
    memory_mb: float | None
    current_job_id: UUID | None


@dataclass(frozen=True)
class QueueHealth:
    active_jobs: int
    max_active_jobs: int
    queued_jobs: int


@dataclass(frozen=True)
class SystemHealth:
    queue: QueueHealth
    workers: tuple[WorkerStatus, ...]
