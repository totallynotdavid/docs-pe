from __future__ import annotations

from uuid import UUID

import msgspec

from fetch.domain.types import Row


# The wire between portal-worker-api and the worker agent, defined once and
# imported by both sides. When these two drifted apart they were a dict literal
# in the handler and a TypedDict in the agent, with nothing to make them agree.


class ClaimRequest(msgspec.Struct, frozen=True):
    sources: tuple[str, ...] = ()

    # The (source, credential_version_id) of the session the requesting lane
    # currently holds open, if any. /claim prefers handing back a matching
    # item so the lane can keep using that session instead of opening a new
    # one; absent (or non-matching) just falls back to plain FIFO.
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


class PublishRequest(msgspec.Struct, frozen=True):
    item_id: UUID
    fence: int

    # Named separately from `content` rather than left inside it: worker-api
    # stores the opaque blob without reading it (see below), but circuit
    # breaker accounting needs exactly these two fields, so they travel as
    # typed protocol fields instead of requiring worker-api to parse a
    # site-shaped payload it otherwise never looks inside.
    source: str
    provider: str
    healthy_contact: bool

    # The queryable content this run confirmed for (document, source),
    # upserted into portal_entries. Typed for the same reason as
    # source/provider above: worker-api never parses `content` for this
    # either. columns/rows stay a generic (names, cells) shape rather than a
    # site-specific one, so this struct never needs to change when a fetch
    # site's fields do -- see portal_entries in 001_portal.sql.
    document: str
    status: str
    columns: tuple[str, ...]
    rows: tuple[Row, ...]
    error_code: str | None

    # Base64: the complete raw result, kept as an opaque archive for audit
    # and replay. Duplicates document/status/columns/rows above by content,
    # never read back out to reconstruct them.
    content: str


class PublishResult(msgspec.Struct, frozen=True):
    published: bool


class HeartbeatRequest(msgspec.Struct, frozen=True):
    """Point-in-time resource usage, not a time series: the admin health page
    only needs to know whether the fleet is healthy right now."""

    cpu_percent: float | None = None
    memory_mb: float | None = None
    current_job_id: UUID | None = None


class EnrollRequest(msgspec.Struct, frozen=True):
    worker_id: str
    tailscale_hostname: str


class EnrollResponse(msgspec.Struct, frozen=True):
    credential: str
