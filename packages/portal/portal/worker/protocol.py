from __future__ import annotations

from uuid import UUID

import msgspec

from core.domain.types import Row


class ClaimRequest(msgspec.Struct, frozen=True):
    sources: tuple[str, ...] = ()

    # The session currently held by the requesting lane, if any.
    affinity_source: str | None = None
    affinity_credential_version_id: UUID | None = None


class CredentialLease(msgspec.Struct, frozen=True):
    """The proxy configuration for one job, in the clear.

    This is the only shape plaintext proxy credentials take outside the
    database, and it exists for exactly one response.
    """

    provider: str
    config: dict[str, str]


class WorkLease(msgspec.Struct, frozen=True):
    """One document to fetch, leased to one worker until the fence moves."""

    item_id: UUID
    job_id: UUID
    source: str
    document: str
    fence: int
    credential_version_id: UUID
    credential: CredentialLease


class AttemptRecord(msgspec.Struct, frozen=True):
    """One fetch_one try, wire shape for portal_lookup_attempts."""

    fetch_attempt: int
    outcome: str
    elapsed_ms: int
    error_code: str | None = None


class PublishRequest(msgspec.Struct, frozen=True):
    item_id: UUID
    fence: int
    lane_index: int

    # These fields drive fleet circuit-breaker accounting. `content` is opaque.
    source: str
    provider: str
    healthy_contact: bool

    # The queryable result for this (document, source) pair.
    document: str
    status: str
    columns: tuple[str, ...]
    rows: tuple[Row, ...]
    error_code: str | None

    # Base64-encoded raw result for audit and replay.
    content: str

    # Every attempt fetch_one made resolving this document, not just the
    # final one: see portal_lookup_attempts (packages/portal/portal/migrations).
    attempts: tuple[AttemptRecord, ...] = ()


class PublishResult(msgspec.Struct, frozen=True):
    published: bool


class ClaimSlotRequest(msgspec.Struct, frozen=True):
    provider: str
    lane_index: int


class ClaimSlotResponse(msgspec.Struct, frozen=True):
    slot_id: int


class ReleaseSlotRequest(msgspec.Struct, frozen=True):
    provider: str
    slot_id: int


class HeldSlot(msgspec.Struct, frozen=True):
    provider: str
    slot_id: int


class HeartbeatRequest(msgspec.Struct, frozen=True):
    """Resource snapshot shown on the admin health page, plus proof of life
    for every proxy slot lease the caller currently holds. A slot missing from
    `held_slots` on the next heartbeat is not renewed and expires on schedule."""

    cpu_percent: float | None = None
    memory_mb: float | None = None
    current_job_id: UUID | None = None
    held_slots: tuple[HeldSlot, ...] = ()


class EnrollRequest(msgspec.Struct, frozen=True):
    worker_id: str
    tailscale_hostname: str


class EnrollResponse(msgspec.Struct, frozen=True):
    credential: str
