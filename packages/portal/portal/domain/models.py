from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID, uuid4


class TeamRole(StrEnum):
    SITE_ADMIN = "site_admin"
    TEAM_LEADER = "team_leader"
    TEAM_MEMBER = "team_member"


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


STABLE_SOURCES = frozenset({"osiptel", "sunat", "sunat_reps"})
ACTIVE_JOB_STATES = frozenset({JobState.RUNNING, JobState.CANCELLING})
TERMINAL_JOB_STATES = frozenset(
    {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
)
PUBLISHED_ITEM_STATES = frozenset({ItemState.PUBLISHED})
MAX_ACTIVE_JOBS = 5


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


@dataclass(frozen=True)
class JobEvent:
    id: UUID
    job_id: UUID
    event_type: str


@dataclass(frozen=True)
class NotificationIntent:
    id: UUID
    event_id: UUID
    channel: DeliveryChannel
    team_id: UUID
