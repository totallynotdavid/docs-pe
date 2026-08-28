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
from fetch.proxy.registry import provider_from_values, spec_for
from fetch.sites.registry import SITES

from portal.worker.protocol import (
    ClaimRequest,
    ClaimSlotRequest,
    ClaimSlotResponse,
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    PublishRequest,
    ReleaseSlotRequest,
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

# Keep the heartbeat shorter than the worker staleness window.
HEARTBEAT_INTERVAL_SECONDS = 15


@dataclass
class LaneSession:
    """Provider session held by a concurrent lane across compatible claims."""

    state: WorkerState = field(default_factory=WorkerState)
    provider: ProxyProvider | None = None
    key: tuple[str, UUID] | None = None
    idle_polls: int = 0
    held_slot: int | None = None
    held_slot_provider: str | None = None


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
    """Claim, execute, and publish work through the worker API.

    Each lane keeps a provider session for compatible claims. Workers hold no
    database credentials.
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
                *(
                    self._loop(client, lane_index)
                    for lane_index in range(self.options.concurrency)
                ),
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

    async def _loop(self, client: httpx.AsyncClient, lane_index: int) -> None:
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
                    await self._idle(client, lane)
                    continue

                try:
                    provider = await self._adopt(client, lane, lease, lane_index)
                    result = await self._execute(lane, lease, provider, lane_index)
                except httpx.HTTPError:
                    # Claiming or releasing a GeoNode slot is a worker-api
                    # call like /claim and /publish, so it can fail the same
                    # transient way and must not take down every other
                    # concurrent lane either. This item's lease is left to
                    # expire and gets swept back to pending for another lane.
                    logger.warning("worker_slot_failed", exc_info=True)
                    await asyncio.sleep(IDLE_POLL_SECONDS)
                    continue

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
            # session or GeoNode slot: every other exit from this loop
            # already routes through _idle/_adopt, which close on their own
            # terms.
            await self._close_and_release(client, lane)

    async def _idle(self, client: httpx.AsyncClient, lane: LaneSession) -> None:
        lane.idle_polls += 1

        if lane.provider is not None and lane.idle_polls >= IDLE_SESSION_CLOSE_AFTER:
            await self._close_and_release(client, lane)
            lane.provider = None
            lane.key = None

        await asyncio.sleep(IDLE_POLL_SECONDS)

    async def _adopt(
        self,
        client: httpx.AsyncClient,
        lane: LaneSession,
        lease: WorkLease,
        lane_index: int,
    ) -> ProxyProvider:
        """Point the lane at this lease's (source, credential), closing
        whatever session/slot it was holding if that's actually changing.

        The held slot is kept for as long as the lane works this provider,
        not reclaimed per session: GeoNode rotates exit identity through a
        fresh session id on the same port, so a session reopening under
        session_budget reuses the slot already held instead of a worker-api
        round trip. See PostgresProxySlots for the lease contract.
        """
        key = (lease.source, lease.credential_version_id)

        if lane.key is not None and lane.key != key:
            await self._close_and_release(client, lane)

        provider = provider_from_values(
            lease.credential.provider, lease.credential.config
        )

        if lane.held_slot is None:
            lane.held_slot = await self._claim_slot(
                client, lease.credential.provider, lane_index
            )
            lane.held_slot_provider = lease.credential.provider

        lane.key = key
        lane.provider = provider
        lane.idle_polls = 0

        return provider

    async def _close_and_release(
        self, client: httpx.AsyncClient, lane: LaneSession
    ) -> None:
        """Stop using this provider entirely: close whatever session is open
        and release the held slot back to the fleet-wide pool. Called when a
        lane switches to a different (source, credential), goes idle, or
        shuts down, not on every session reopen. Safe to call whether or not
        either is currently held."""
        if lane.provider is not None:
            with contextlib.suppress(Exception):
                await close_session(lane.state, provider=lane.provider)

        if lane.held_slot is not None and lane.held_slot_provider is not None:
            with contextlib.suppress(Exception):
                await self._release_slot(
                    client, lane.held_slot_provider, lane.held_slot
                )
            lane.held_slot = None
            lane.held_slot_provider = None

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
        self,
        lane: LaneSession,
        lease: WorkLease,
        provider: ProxyProvider,
        lane_index: int,
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

        # _adopt runs before _execute in the loop and always sets a slot.
        assert lane.held_slot is not None

        result = await fetch_one(
            site=site,
            state=lane.state,
            doc=Doc(lease.document),
            provider=provider,
            breaker=breaker,
            slot_id=lane.held_slot,
            run_id=self.options.worker_id,
            lane_id=lane_index + 1,
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

    async def _claim_slot(
        self, client: httpx.AsyncClient, provider_name: str, lane_index: int
    ) -> int:
        """A provider's slot_id only needs fleet-wide uniqueness when it maps
        to a real shared resource (GeoNode's sticky ports). Everything else
        gets a cheap, purely local assignment with no worker-api round trip."""
        if spec_for(provider_name).tuning.slot_pool is None:
            return lane_index + 1

        while True:
            response = await client.post(
                "/claim-slot",
                content=msgspec.json.encode(
                    ClaimSlotRequest(provider=provider_name, lane_index=lane_index)
                ),
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()

            claimed = msgspec.json.decode(
                response.content, type=ClaimSlotResponse | None
            )

            if claimed is not None:
                return claimed.slot_id

            # Every real slot is leased to a still-live worker somewhere in
            # the fleet: back off and retry rather than fail this lookup.
            await asyncio.sleep(IDLE_POLL_SECONDS)

    async def _release_slot(
        self, client: httpx.AsyncClient, provider_name: str, slot_id: int
    ) -> None:
        if spec_for(provider_name).tuning.slot_pool is None:
            return

        response = await client.post(
            "/release-slot",
            content=msgspec.json.encode(
                ReleaseSlotRequest(provider=provider_name, slot_id=slot_id)
            ),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()


async def self_enroll(
    worker_api_url: str,
    bootstrap_token: str,
    worker_id: str,
    tailscale_hostname: str,
) -> str:
    """Mint a worker credential from the enrollment endpoint."""
    # Enrollment is a control-plane request. Retry transient connection
    # failures, including after a worker restart.
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
