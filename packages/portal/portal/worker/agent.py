from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import logging
import os
import signal
import sys
import time

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, TypedDict

import asyncpg
import httpx
import msgspec
import psutil

from core.domain.types import Cell, Doc
from core.pipeline.breaker import CircuitBreaker
from core.pipeline.fetch import fetch_one
from core.pipeline.session import WorkerConfig, WorkerState, close_session
from core.proxy.registry import provider_from_values, spec_for
from core.sites.registry import SITES

from portal.repository.jobs import PostgresJobRepository
from portal.repository.slots import PostgresProxySlots
from portal.repository.workers import PostgresWorkerRegistry
from portal.worker.protocol import (
    AttemptRecord,
    CredentialLease,
    EnrollRequest,
    EnrollResponse,
    PublishRequest,
    RevealCredentialRequest,
    WorkLease,
)


if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID

    from core.proxy.base import ProxyProvider

    from portal.domain.models import ClaimedWork


logger = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 4

# Close a sticky session after repeated empty dispatch cycles.
IDLE_SESSION_CLOSE_AFTER = 5

# Keep the heartbeat shorter than the worker staleness window.
HEARTBEAT_INTERVAL_SECONDS = 15

# Polling catches a missed notification and a breaker reopening without a row
# change.
DISPATCH_BASE_BACKOFF_SECONDS = 5.0
DISPATCH_MAX_BACKOFF_SECONDS = 20.0

DISPATCH_DEBOUNCE_SECONDS = 0.25

LISTEN_CHANNEL = "portal_work_available"
LISTEN_RECONNECT_BASE_SECONDS = 1.0
LISTEN_RECONNECT_MAX_SECONDS = 30.0

# Expire cached credentials so rotation takes effect without a worker restart.
CREDENTIAL_CACHE_SIZE = 32
CREDENTIAL_CACHE_TTL_SECONDS = 300

# How long a slot-claim retry waits when every real slot is leased elsewhere.
SLOT_WAIT_SECONDS = 2


@dataclass
class LaneSession:
    """Provider session held by a concurrent lane across compatible claims."""

    state: WorkerState = field(default_factory=WorkerState)
    provider: ProxyProvider | None = None
    key: tuple[str, UUID] | None = None
    idle_polls: int = 0
    held_slot: int | None = None
    held_slot_provider: str | None = None


class AttemptResult(TypedDict):
    fetch_attempt: int
    outcome: str
    elapsed_ms: int
    error_code: str | None


class ExecuteResult(TypedDict):
    """The shape sent to /publish, both as portal_entries' typed fields and,
    unmodified, as the archived content blob."""

    document: str
    source: str
    status: str
    columns: list[str]
    rows: list[list[Cell]]
    error_code: str | None
    attempts: list[AttemptResult]


@dataclass(frozen=True)
class AgentOptions:
    worker_api_url: str
    credential: str
    database_dsn: str
    worker_id: str
    sources: tuple[str, ...]
    concurrency: int = DEFAULT_CONCURRENCY


class WorkerAgent:
    """Claim, execute, and publish work through the worker API and Postgres."""

    def __init__(self, options: AgentOptions) -> None:
        self.options = options
        self._breakers: dict[tuple[str, str], CircuitBreaker] = {}
        # The health page shows the most recently claimed job, not per-lane state.
        self._current_job_id: UUID | None = None
        self._lanes = [LaneSession() for _ in range(options.concurrency)]
        self._queues: list[asyncio.Queue[ClaimedWork]] = [
            asyncio.Queue(maxsize=1) for _ in range(options.concurrency)
        ]
        self._idle_lanes: set[int] = set()
        self._wake = asyncio.Event()
        self._credential_cache: OrderedDict[UUID, tuple[CredentialLease, float]] = (
            OrderedDict()
        )

    async def run(self) -> None:
        """Run the worker with resources created from its configuration."""
        pool = await asyncpg.create_pool(
            self.options.database_dsn,
            min_size=2,
            max_size=max(4, self.options.concurrency + 2),
        )

        headers = {
            "Authorization": f"Bearer {self.options.credential}",
            "X-Portal-Worker": self.options.worker_id,
        }

        # Avoid reusing an idle HTTP connection after a long lookup.
        transport = httpx.AsyncHTTPTransport(retries=2)
        limits = httpx.Limits(max_keepalive_connections=0)

        try:
            async with httpx.AsyncClient(
                base_url=self.options.worker_api_url,
                headers=headers,
                timeout=90,
                transport=transport,
                limits=limits,
            ) as client:
                await self.run_with(pool, client)
        finally:
            await pool.close()

    async def run_with(self, pool: asyncpg.Pool, client: httpx.AsyncClient) -> None:
        jobs = PostgresJobRepository(pool)
        workers = PostgresWorkerRegistry(pool)
        slots = PostgresProxySlots(pool)

        tasks = [
            asyncio.create_task(self._heartbeat_loop(workers, slots)),
            asyncio.create_task(self._listen_loop()),
            asyncio.create_task(self._dispatch_loop(jobs)),
            *(
                asyncio.create_task(self._lane_loop(client, slots, lane_index))
                for lane_index in range(self.options.concurrency)
            ),
        ]

        # Cancellation lets each lane release its session and slot before the
        # container stop grace period ends.
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)

        stop_waiter = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait(
                [stop_waiter, *tasks], return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stop_waiter.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _heartbeat_loop(
        self, workers: PostgresWorkerRegistry, slots: PostgresProxySlots
    ) -> None:
        process = psutil.Process()
        process.cpu_percent()  # First call only primes the internal baseline.

        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)

            # Renew only the slots still held by these lanes. A removed slot
            # must expire instead of being renewed by stale process state.
            held_slots = tuple(
                (lane.held_slot_provider, lane.held_slot)
                for lane in self._lanes
                if lane.held_slot is not None and lane.held_slot_provider is not None
            )

            try:
                await workers.record_heartbeat(
                    self.options.worker_id,
                    cpu_percent=process.cpu_percent(),
                    memory_mb=process.memory_info().rss / (1024 * 1024),
                    current_job_id=self._current_job_id,
                )
                await slots.renew(worker_id=self.options.worker_id, held=held_slots)
            except (asyncpg.PostgresError, OSError):
                # A missed heartbeat just makes the worker look briefly stale;
                # it must never take down the claim/execute/publish loops.
                logger.warning("worker_heartbeat_failed", exc_info=True)

    async def _listen_loop(self) -> None:
        """Wake the dispatcher when PostgreSQL reports claimable work."""
        delay = LISTEN_RECONNECT_BASE_SECONDS

        while True:
            try:
                connection = await asyncpg.connect(self.options.database_dsn)
            except (asyncpg.PostgresError, OSError):
                logger.warning("worker_listen_connect_failed", exc_info=True)
                await asyncio.sleep(delay)
                delay = min(delay * 2, LISTEN_RECONNECT_MAX_SECONDS)
                continue

            delay = LISTEN_RECONNECT_BASE_SECONDS

            try:
                await self._listen_until_terminated(connection)
            except (asyncpg.PostgresError, OSError):
                logger.warning("worker_listen_failed", exc_info=True)
            finally:
                with contextlib.suppress(Exception):
                    await connection.close()

    async def _listen_until_terminated(self, connection: asyncpg.Connection) -> None:
        terminated = asyncio.Event()

        def _on_terminate(*_args: object) -> None:
            terminated.set()

        await connection.add_listener(LISTEN_CHANNEL, lambda *_args: self._wake.set())
        connection.add_termination_listener(_on_terminate)
        self._wake.set()
        await terminated.wait()

    async def _dispatch_loop(self, jobs: PostgresJobRepository) -> None:
        backoff = DISPATCH_BASE_BACKOFF_SECONDS

        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=backoff)
            self._wake.clear()

            await asyncio.sleep(DISPATCH_DEBOUNCE_SECONDS)
            self._wake.clear()

            try:
                claimed_total = await self._dispatch_once(jobs)
            except (asyncpg.PostgresError, OSError):
                claimed_total = 0
                logger.warning("worker_claim_failed", exc_info=True)

            backoff = (
                DISPATCH_BASE_BACKOFF_SECONDS
                if claimed_total
                else min(backoff * 2, DISPATCH_MAX_BACKOFF_SECONDS)
            )

    async def _dispatch_once(self, jobs: PostgresJobRepository) -> int:
        idle = list(self._idle_lanes)

        if not idle:
            return 0

        groups: dict[tuple[str, UUID] | None, list[int]] = {}

        for lane_index in idle:
            groups.setdefault(self._lanes[lane_index].key, []).append(lane_index)

        claimed_total = 0
        remaining: list[int] = []

        for key, lane_indices in groups.items():
            if key is None:
                remaining.extend(lane_indices)
                continue

            claimed = await jobs.claim_many(
                self.options.worker_id,
                self.options.sources,
                len(lane_indices),
                affinity_source=key[0],
                affinity_credential_version_id=key[1],
            )
            claimed_total += len(claimed)
            remaining.extend(self._assign(lane_indices, claimed))

        if remaining:
            claimed = await jobs.claim_many(
                self.options.worker_id, self.options.sources, len(remaining)
            )
            claimed_total += len(claimed)

            for lane_index in self._assign(remaining, claimed):
                self._lanes[lane_index].idle_polls += 1

        return claimed_total

    def _assign(
        self, lane_indices: list[int], claimed: tuple[ClaimedWork, ...]
    ) -> list[int]:
        """Hand each claimed item to one idle lane."""
        for lane_index, work in zip(lane_indices, claimed, strict=False):
            self._queues[lane_index].put_nowait(work)
            self._idle_lanes.discard(lane_index)
            self._lanes[lane_index].idle_polls = 0

        return lane_indices[len(claimed) :]

    async def _lane_loop(
        self,
        client: httpx.AsyncClient,
        slots: PostgresProxySlots,
        lane_index: int,
    ) -> None:
        lane = self._lanes[lane_index]
        queue = self._queues[lane_index]

        try:
            while True:
                self._idle_lanes.add(lane_index)

                if (
                    lane.provider is not None
                    and lane.idle_polls >= IDLE_SESSION_CLOSE_AFTER
                ):
                    await self._close_and_release(client, slots, lane)
                    lane.provider = None
                    lane.key = None

                claimed = await queue.get()

                try:
                    lease = await self._adopt(client, slots, lane, claimed, lane_index)
                    assert lane.provider is not None
                    result = await self._execute(lane, lease, lane.provider, lane_index)
                    await self._publish(client, lease, result, lane_index)
                except (asyncpg.PostgresError, httpx.HTTPError, OSError):
                    # Leave this item for lease expiry and let another lane
                    # retry it. One lane's failure must not stop the others.
                    logger.warning("worker_lane_failed", exc_info=True)
                finally:
                    self._wake.set()
        finally:
            self._idle_lanes.discard(lane_index)
            await self._close_and_release(client, slots, lane)

    async def _adopt(
        self,
        client: httpx.AsyncClient,
        slots: PostgresProxySlots,
        lane: LaneSession,
        claimed: ClaimedWork,
        lane_index: int,
    ) -> WorkLease:
        """Use this claim's provider credential and close a changed one."""
        key = (claimed.source, claimed.credential_version_id)

        if lane.key is not None and lane.key != key:
            await self._close_and_release(client, slots, lane)

        credential = await self._reveal_credential(
            client, claimed.credential_version_id
        )
        provider = provider_from_values(credential.provider, credential.config)

        if lane.held_slot is None:
            lane.held_slot = await self._claim_slot(
                slots, credential.provider, lane_index
            )
            lane.held_slot_provider = credential.provider

        lane.key = key
        lane.provider = provider
        lane.idle_polls = 0
        self._current_job_id = claimed.job_id

        return WorkLease(
            item_id=claimed.item_id,
            job_id=claimed.job_id,
            source=claimed.source,
            document=claimed.document,
            fence=claimed.lease_fence,
            credential_version_id=claimed.credential_version_id,
            credential=credential,
        )

    async def _reveal_credential(
        self, client: httpx.AsyncClient, credential_version_id: UUID
    ) -> CredentialLease:
        cached = self._credential_cache.get(credential_version_id)

        if cached is not None:
            credential, expires_at = cached

            if time.monotonic() < expires_at:
                self._credential_cache.move_to_end(credential_version_id)
                return credential

            # Re-reveal after the TTL so credential rotation reaches this worker.
            del self._credential_cache[credential_version_id]

        response = await client.post(
            "/reveal-credential",
            content=msgspec.json.encode(
                RevealCredentialRequest(credential_version_id=credential_version_id)
            ),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()

        credential = msgspec.json.decode(response.content, type=CredentialLease)

        self._credential_cache[credential_version_id] = (
            credential,
            time.monotonic() + CREDENTIAL_CACHE_TTL_SECONDS,
        )
        if len(self._credential_cache) > CREDENTIAL_CACHE_SIZE:
            self._credential_cache.popitem(last=False)

        return credential

    async def _close_and_release(
        self,
        client: httpx.AsyncClient,
        slots: PostgresProxySlots,
        lane: LaneSession,
    ) -> None:
        """Close the lane session and release its provider slot, if held."""
        if lane.provider is not None:
            with contextlib.suppress(Exception):
                await close_session(lane.state, provider=lane.provider)

        if lane.held_slot is not None and lane.held_slot_provider is not None:
            try:
                await self._release_slot(slots, lane.held_slot_provider, lane.held_slot)
            except (asyncpg.PostgresError, OSError):
                # Keep the local claim so a later cleanup can retry the
                # idempotent release while the lane may still use the slot.
                pass
            else:
                lane.held_slot = None
                lane.held_slot_provider = None

    async def _publish(
        self,
        client: httpx.AsyncClient,
        lease: WorkLease,
        result: ExecuteResult,
        lane_index: int,
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
                    lane_index=lane_index,
                    source=lease.source,
                    provider=lease.credential.provider,
                    healthy_contact=result["status"] != "failed",
                    document=result["document"],
                    status=result["status"],
                    columns=tuple(result["columns"]),
                    rows=tuple(tuple(row) for row in result["rows"]),
                    error_code=result["error_code"],
                    content=content,
                    attempts=tuple(
                        AttemptRecord(
                            fetch_attempt=a["fetch_attempt"],
                            outcome=a["outcome"],
                            elapsed_ms=a["elapsed_ms"],
                            error_code=a["error_code"],
                        )
                        for a in result["attempts"]
                    ),
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
                provider=provider_name,
                source=lease.source,
                run_id=self.options.worker_id,
            ),
        )

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
            "attempts": [
                {
                    "fetch_attempt": a.attempt,
                    "outcome": a.status.value,
                    "elapsed_ms": a.elapsed_ms,
                    "error_code": a.error_code or None,
                }
                for a in result.attempts
            ],
        }

    async def _claim_slot(
        self, slots: PostgresProxySlots, provider_name: str, lane_index: int
    ) -> int:
        """Return a fleet slot for providers that use a shared pool."""
        if spec_for(provider_name).tuning.slot_pool is None:
            return lane_index + 1

        while True:
            slot_id = await slots.claim(
                provider=provider_name,
                worker_id=self.options.worker_id,
                lane_index=lane_index,
            )

            if slot_id is not None:
                return slot_id

            # Every real slot is leased to a still-live worker somewhere in
            # the fleet: back off and retry rather than fail this lookup.
            await asyncio.sleep(SLOT_WAIT_SECONDS)

    async def _release_slot(
        self, slots: PostgresProxySlots, provider_name: str, slot_id: int
    ) -> None:
        if spec_for(provider_name).tuning.slot_pool is None:
            return

        await slots.release(
            provider=provider_name,
            slot_id=slot_id,
            worker_id=self.options.worker_id,
        )


async def self_enroll(
    worker_api_url: str,
    bootstrap_token: str,
    worker_id: str,
    tailscale_hostname: str,
) -> tuple[str, str]:
    """Mint a worker credential and direct-DB role from the enrollment endpoint."""
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

        enrolled = msgspec.json.decode(response.content, type=EnrollResponse)
        return enrolled.credential, enrolled.database_dsn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worker",
        description="Claim documents directly from Postgres and publish results.",
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
    parser.add_argument(
        "--database-dsn",
        default=os.environ.get("PORTAL_WORKER_DATABASE_DSN", ""),
        help="Fixed direct-DB DSN, paired with --credential. Ignored when "
        "self-enrolling, which mints both on every start.",
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

        credential, database_dsn = asyncio.run(
            self_enroll(
                args.worker_api_url.rstrip("/"),
                args.bootstrap_token,
                args.worker_id,
                args.tailscale_hostname,
            )
        )
    else:
        credential = os.environ.get("PORTAL_WORKER_CREDENTIAL", "")
        database_dsn = args.database_dsn
        if not credential or not database_dsn:
            raise SystemExit(
                "PORTAL_WORKER_BOOTSTRAP_TOKEN (to self-enroll) or both "
                "PORTAL_WORKER_CREDENTIAL and PORTAL_WORKER_DATABASE_DSN "
                "(issued with `portal-admin worker issue`) are required."
            )

    sources = tuple(value.strip() for value in args.sources.split(",") if value.strip())
    if not sources:
        raise SystemExit("at least one source is required")

    if args.concurrency < 1:
        raise SystemExit("concurrency must be at least 1")

    options = AgentOptions(
        worker_api_url=args.worker_api_url.rstrip("/"),
        credential=credential,
        database_dsn=database_dsn,
        worker_id=args.worker_id,
        sources=sources,
        concurrency=args.concurrency,
    )

    asyncio.run(WorkerAgent(options).run())


def main() -> None:
    run(sys.argv[1:])


if __name__ == "__main__":
    main()
