from __future__ import annotations

from uuid import UUID

import msgspec


# The wire between portal-worker-api and the worker agent, defined once and
# imported by both sides. When these two drifted apart they were a dict literal
# in the handler and a TypedDict in the agent, with nothing to make them agree.


class ClaimRequest(msgspec.Struct, frozen=True):
    sources: tuple[str, ...] = ()


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
    credential: CredentialLease


class PublishRequest(msgspec.Struct, frozen=True):
    item_id: UUID
    fence: int

    # Base64 rather than a nested object: the payload is a fetch result whose
    # shape belongs to the site, and the API stores it without reading it.
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
