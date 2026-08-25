from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import logging
import os

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

import httpx
import msgspec
import psutil

from fetch.domain.types import Cell, Doc
from fetch.pipeline.breaker import CircuitBreaker
from fetch.pipeline.fetch import fetch_one
from fetch.pipeline.session import WorkerConfig, WorkerState, close_session
from fetch.proxy.registry import provider_from_values
from fetch.sites.registry import SITES

from portal.worker.protocol import (
    ClaimRequest,
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    PublishRequest,
    WorkLease,
)


if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from fetch.proxy.base import ProxyProvider


logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 4
IDLE_POLL_SECONDS = 2

# How many consecutive empty claims (no matching work anywhere) a lane waits
# before releasing a session it's holding open. Keeps a quiet lane from
# sitting on a sticky proxy session indefinitely while nothing needs it.
IDLE_SESSION_CLOSE_AFTER = 5

# 1/3 of repository/workers.py's HEARTBEAT_STALE_AFTER, so a single missed
# beat (network blip, slow request) doesn't flip a healthy worker offline.
HEARTBEAT_INTERVAL_SECONDS = 15


@dataclass
class LaneSession:
    """One lane's held session, if any, across consecutive claims.

    A lane is one concurrent slot in the fleet, not one document: it keeps a
    provider session open across consecutive claims of the same
    (source, credential_version_id) so fetch.pipeline.session's session_budget
    is actually amortized the way the standalone fetch CLI amortizes it,
    instead of every claimed document paying full session-open cost.
    """

    state: WorkerState = field(default_factory=WorkerState)
    provider: ProxyProvider | None = None
    key: tuple[str, UUID] | None = None
    idle_polls: int = 0


class ExecuteResult(TypedDict):
    """The shape sent to /publish, both as portal_entries' typed fields and,
    unmodified, as the archived content blob."""

    document: str
    source: str
    status: str
    columns: list[str]
    rows: list[list[Cell]]
    error_code: str | None


@dataclass(frozen=True)
class AgentOptions:
    worker_api_url: str
    credential: str
    worker_id: str
    sources: tuple[str, ...]
    concurrency: int = DEFAULT_CONCURRENCY


class WorkerAgent:
    """Claims documents from portal-worker-api, one at a time, and publishes them.

    Each concurrent lane keeps its own LaneSession across claims: as long as
    consecutive claims stay within the same (source, credential_version_id),
    the lane's provider session lives on instead of being closed and reopened
    per document, so fetch.pipeline.session's session_budget rotation applies
    the same way it does in the standalone fetch CLI.

    The agent holds no database credentials by design: a compromised browser
    automation node gets the job it is holding and the proxy credential for that
    job, and nothing else in the installation.
    """

    def __init__(self, options: AgentOptions) -> None:
        self.options = options
        self._breakers: dict[tuple[str, str], CircuitBreaker] = {}
        # Best-effort signal for the admin health page, not correctness-critical:
        # concurrent lanes can each be mid-flight on a different job, so this is
        # whichever one claimed most recently, not a per-lane breakdown.
        self._current_job_id: UUID | None = None

    async def run(self) -> None:
        headers = {
            "Authorization": f"Bearer {self.options.credential}",
            "X-Portal-Worker": self.options.worker_id,
        }

        # A relayed/latent tailnet path lets a pooled keep-alive connection sit
        # idle long enough (a lookup can take tens of seconds) that the peer or
        # an in-between relay tears it down; the next request reuses it from the
        # pool and fails with RemoteProtocolError before a single byte comes
        # back. httpx's own `retries` only covers failures establishing a fresh
        # connection (httpcore.ConnectionPool's docstring is explicit about
        # this) and does nothing for a request that fails on a connection
        # pulled back out of the pool, confirmed live: it kept happening with
        # retries=2 set. max_keepalive_connections=0 sidesteps the whole class
        # by never reusing a connection across requests, which costs an extra
        # handshake per call but these are infrequent control-plane calls, not
        # a hot path.
        transport = httpx.AsyncHTTPTransport(retries=2)
        limits = httpx.Limits(max_keepalive_connections=0)

        async with httpx.AsyncClient(
            base_url=self.options.worker_api_url,
            headers=headers,
            timeout=90,
            transport=transport,
            limits=limits,
        ) as client:
            await asyncio.gather(
                self._heartbeat_loop(client),
                *(self._loop(client) for _ in range(self.options.concurrency)),
            )

    async def _heartbeat_loop(self, client: httpx.AsyncClient) -> None:
        process = psutil.Process()
        process.cpu_percent()  # First call only primes the internal baseline.

        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

            try:
                response = await client.post(
                    "/heartbeat",
                    content=msgspec.json.encode(
                        HeartbeatRequest(
                            cpu_percent=process.cpu_percent(),
                            memory_mb=process.memory_info().rss / (1024 * 1024),
                            current_job_id=self._current_job_id,
                        )
                    ),
                    headers={"Content-Type": "application/json"},
                )
                response.raise_for_status()
            except httpx.HTTPError:
                # A missed heartbeat just makes the worker look briefly stale;
                # it must never take down the claim/execute/publish loops.
                pass

    async def _loop(self, client: httpx.AsyncClient) -> None:
        lane = LaneSession()

        try:
            while True:
                try:
                    lease = await self._claim(client, lane.key)
                except httpx.HTTPError:
                    # A single lane's claim failing (worker-api briefly
                    # unreachable, a credential mid-rotation) must not take
                    # down the other concurrent lanes or the heartbeat: same
                    # principle as _heartbeat_loop's own catch, applied here
                    # too.
                    logger.warning("worker_claim_failed", exc_info=True)
                    await asyncio.sleep(IDLE_POLL_SECONDS)
                    continue

                if lease is None:
                    await self._idle(lane)
                    continue

                provider = await self._adopt(lane, lease)

                result = await self._execute(lane, lease, provider)

                try:
                    await self._publish(client, lease, result)
                except httpx.HTTPError:
                    # The lease may already be gone (reclaimed under a stale
                    # fence after this took too long, or the credential
                    # rotated mid-flight): the result for this attempt is
                    # lost, but crashing every other concurrent lane over one
                    # rejected publish is worse. Whoever holds the lease now
                    # redoes it.
                    logger.warning("worker_publish_failed", exc_info=True)
        finally:
            # Cancellation (process shutdown) must not leak a held sticky
            # session: every other exit from this loop already routes through
            # _idle/_adopt, which close on their own terms.
            if lane.provider is not None:
                with contextlib.suppress(Exception):
                    await close_session(lane.state, provider=lane.provider)

    async def _idle(self, lane: LaneSession) -> None:
        lane.idle_polls += 1

        if lane.provider is not None and lane.idle_polls >= IDLE_SESSION_CLOSE_AFTER:
            with contextlib.suppress(Exception):
                await close_session(lane.state, provider=lane.provider)
            lane.provider = None
            lane.key = None

        await asyncio.sleep(IDLE_POLL_SECONDS)

    async def _adopt(self, lane: LaneSession, lease: WorkLease) -> ProxyProvider:
        """Point the lane at this lease's (source, credential), closing
        whatever session it was holding if that's actually changing."""
        key = (lease.source, lease.credential_version_id)

        if lane.key is not None and lane.key != key and lane.provider is not None:
            with contextlib.suppress(Exception):
                await close_session(lane.state, provider=lane.provider)

        provider = provider_from_values(
            lease.credential.provider, lease.credential.config
        )
        lane.key = key
        lane.provider = provider
        lane.idle_polls = 0

        return provider

    async def _claim(
        self, client: httpx.AsyncClient, affinity: tuple[str, UUID] | None
    ) -> WorkLease | None:
        response = await client.post(
            "/claim",
            content=msgspec.json.encode(
                ClaimRequest(
                    sources=self.options.sources,
                    affinity_source=affinity[0] if affinity else None,
                    affinity_credential_version_id=affinity[1] if affinity else None,
                )
            ),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()

        lease = msgspec.json.decode(response.content, type=WorkLease | None)

        if lease is not None:
            self._current_job_id = lease.job_id

        return lease

    async def _publish(
        self,
        client: httpx.AsyncClient,
        lease: WorkLease,
        result: ExecuteResult,
    ) -> None:
        content = base64.b64encode(
            json.dumps(result, separators=(",", ":")).encode()
        ).decode()

        response = await client.post(
            "/publish",
            content=msgspec.json.encode(
                PublishRequest(
                    item_id=lease.item_id,
                    fence=lease.fence,
                    source=lease.source,
                    provider=lease.credential.provider,
                    healthy_contact=result["status"] != "failed",
                    document=result["document"],
                    status=result["status"],
                    columns=tuple(result["columns"]),
                    rows=tuple(tuple(row) for row in result["rows"]),
                    error_code=result["error_code"],
                    content=content,
                )
            ),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()

    async def _execute(
        self, lane: LaneSession, lease: WorkLease, provider: ProxyProvider
    ) -> ExecuteResult:
        provider_name = lease.credential.provider
        site = SITES[lease.source]

        breaker = self._breakers.setdefault(
            (lease.source, provider_name),
            CircuitBreaker(
                provider=f"{lease.source}:{provider_name}",
                run_id=self.options.worker_id,
            ),
        )

        result = await fetch_one(
            site=site,
            state=lane.state,
            doc=Doc(lease.document),
            provider=provider,
            breaker=breaker,
            slot_id=1,
            run_id=self.options.worker_id,
            lane_id=1,
            cfg=WorkerConfig(
                session_budget=site.tuning.session_budget,
                wait_min_s=0,
                wait_max_s=0,
                ban_cooldown_s=provider.tuning.ban_cooldown_s,
            ),
        )

        return {
            "document": lease.document,
            "source": lease.source,
            "status": result.status.value,
            "columns": list(site.columns),
            "rows": [list(row) for row in result.rows],
            "error_code": result.error_code,
        }


async def self_enroll(
    worker_api_url: str,
    bootstrap_token: str,
    worker_id: str,
    tailscale_hostname: str,
) -> str:
    """Mint a fresh credential from portal-worker-api's /enroll endpoint.

    Called on every start, not just the first: issuing is idempotent by
    worker_id, so this replaces `portal enroll-worker` and a copy-pasted,
    shown-once credential with a value that never has to be persisted on the
    node at all.
    """
    # Same connection-retry as WorkerAgent.run(): this runs fresh on every
    # process start, including every restart of a crash-looping container, so
    # it can't afford to be the one call with no resilience to a torn-down
    # keep-alive connection.
    transport = httpx.AsyncHTTPTransport(retries=2)

    async with httpx.AsyncClient(
        base_url=worker_api_url, timeout=30, transport=transport
    ) as client:
        response = await client.post(
            "/enroll",
            content=msgspec.json.encode(
                EnrollRequest(
                    worker_id=worker_id, tailscale_hostname=tailscale_hostname
                )
            ),
            headers={
                "Authorization": f"Bearer {bootstrap_token}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()

        return msgspec.json.decode(response.content, type=EnrollResponse).credential


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="portal worker",
        description="Claim documents from portal-worker-api and publish results.",
    )
    parser.add_argument(
        "--worker-api-url",
        default=os.environ.get("PORTAL_WORKER_API_URL", ""),
    )
    parser.add_argument(
        "--worker-id",
        default=os.environ.get("PORTAL_WORKER_ID", "poseidon-1"),
    )
    parser.add_argument(
        "--bootstrap-token",
        default=os.environ.get("PORTAL_WORKER_BOOTSTRAP_TOKEN", ""),
        help="Self-enroll on every start instead of using a fixed credential.",
    )
    parser.add_argument(
        "--tailscale-hostname",
        default=os.environ.get("PORTAL_WORKER_TAILSCALE_HOSTNAME", ""),
        help="This node's tailnet hostname. Required to self-enroll.",
    )
    parser.add_argument("--sources", default="sunat,osiptel,sunat_reps")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("PORTAL_WORKER_CONCURRENCY", DEFAULT_CONCURRENCY)),
    )
    return parser


def run(argv: Sequence[str]) -> None:
    args = build_parser().parse_args(argv)

    if not args.worker_api_url:
        raise SystemExit("PORTAL_WORKER_API_URL is required.")

    if args.bootstrap_token:
        if not args.tailscale_hostname:
            raise SystemExit(
                "PORTAL_WORKER_TAILSCALE_HOSTNAME is required to self-enroll."
            )

        credential = asyncio.run(
            self_enroll(
                args.worker_api_url.rstrip("/"),
                args.bootstrap_token,
                args.worker_id,
                args.tailscale_hostname,
            )
        )
    else:
        credential = os.environ.get("PORTAL_WORKER_CREDENTIAL", "")
        if not credential:
            raise SystemExit(
                "PORTAL_WORKER_BOOTSTRAP_TOKEN (to self-enroll) or "
                "PORTAL_WORKER_CREDENTIAL (issued with `portal enroll-worker`) "
                "is required."
            )

    sources = tuple(value.strip() for value in args.sources.split(",") if value.strip())
    if not sources:
        raise SystemExit("at least one source is required")

    if args.concurrency < 1:
        raise SystemExit("concurrency must be at least 1")

    options = AgentOptions(
        worker_api_url=args.worker_api_url.rstrip("/"),
        credential=credential,
        worker_id=args.worker_id,
        sources=sources,
        concurrency=args.concurrency,
    )

    asyncio.run(WorkerAgent(options).run())
