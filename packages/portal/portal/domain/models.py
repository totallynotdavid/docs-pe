from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4


class TeamRole(StrEnum):
    TEAM_LEADER = "team_leader"
    TEAM_MEMBER = "team_member"


class CredentialState(StrEnum):
    """Durable lifecycle for a team-owned proxy credential version."""

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
# Job events a member is notified about. Progress events stay out of the feed.
TERMINAL_JOB_EVENTS = ("proceso.completed", "proceso.failed", "proceso.cancelled")
MAX_ACTIVE_JOBS = 5
# Times an item may be handed to a worker before an expired lease retires it. This
# bounds worker handoffs, not lookup attempts: the engine caps those separately.
MAX_LEASE_ATTEMPTS = 4


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


@dataclass(frozen=True)
class CredentialVersion:
    id: UUID
    team_id: UUID
    label: str
    version: int
    is_active: bool = True
    state: CredentialState = CredentialState.ACTIVE
    # A key into fetch.proxy.registry.PROVIDERS; the engine owns that vocabulary.
    provider: str = ""


@dataclass(frozen=True)
class PortalUser:
    """A browser identity. Password material never leaves the repository."""

    id: UUID
    email: str
    is_site_admin: bool = False


@dataclass(frozen=True)
class BrowserSession:
    """Authenticated server-side session data; only its opaque ID is a cookie."""

    user: PortalUser
    csrf_token: str


@dataclass(frozen=True)
class Team:
    id: UUID
    slug: str
    name: str
    role: TeamRole | None = None


@dataclass(frozen=True)
class SearchResult:
    job_id: UUID
    filename: str
    document: str


@dataclass
class JobItem:
    id: UUID = field(default_factory=uuid4)
    ordinal: int = 0
    document: str = ""
    source: str = ""
    state: ItemState = ItemState.PENDING
    lease_fence: int = 0
    result_object_id: UUID | None = None


@dataclass(frozen=True)
class ClaimedWork:
    """A PostgreSQL-fenced item handed to one portal worker."""

    item_id: UUID
    job_id: UUID
    source: str
    document: str
    lease_fence: int


@dataclass(frozen=True)
class JobCredential:
    """The proxy credential a job's items must be fetched through.

    Stays encrypted until the boundary that hands work to a worker, so the
    repository never holds plaintext proxy passwords.
    """

    provider: str
    config_ciphertext: bytes


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
