from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import msgspec
import psutil

from fetch.domain.types import Doc
from fetch.pipeline.breaker import CircuitBreaker
from fetch.pipeline.fetch import fetch_one
from fetch.pipeline.session import WorkerConfig, WorkerState, close_session
from fetch.proxy.registry import provider_from_values
from fetch.sites.registry import SITES

from portal.worker.protocol import HeartbeatRequest, PublishRequest, WorkLease


if TYPE_CHECKING:
    from collections.abc import Sequence
    from uuid import UUID


DEFAULT_CONCURRENCY = 4
IDLE_POLL_SECONDS = 2

# 1/3 of repository/workers.py's HEARTBEAT_STALE_AFTER, so a single missed
# beat (network blip, slow request) doesn't flip a healthy worker offline.
HEARTBEAT_INTERVAL_SECONDS = 15


@dataclass(frozen=True)
class AgentOptions:
    worker_api_url: str
    credential: str
    worker_id: str
    sources: tuple[str, ...]
    concurrency: int = DEFAULT_CONCURRENCY


class WorkerAgent:
    """Claims one document at a time from portal-worker-api and publishes it.

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

        async with httpx.AsyncClient(
            base_url=self.options.worker_api_url,
            headers=headers,
            timeout=90,
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
        while True:
            lease = await self._claim(client)

            if lease is None:
                await asyncio.sleep(IDLE_POLL_SECONDS)
                continue

            result = await self._execute(lease)

            await self._publish(client, lease, result)

    async def _claim(self, client: httpx.AsyncClient) -> WorkLease | None:
        response = await client.post(
            "/claim",
            json={"sources": list(self.options.sources)},
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
        result: dict[str, object],
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
                    content=content,
                )
            ),
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()

    async def _execute(self, lease: WorkLease) -> dict[str, object]:
        provider_name = lease.credential.provider
        provider = provider_from_values(provider_name, lease.credential.config)
        site = SITES[lease.source]

        breaker = self._breakers.setdefault(
            (lease.source, provider_name),
            CircuitBreaker(
                provider=f"{lease.source}:{provider_name}",
                run_id=self.options.worker_id,
            ),
        )

        state = WorkerState()

        try:
            result = await fetch_one(
                site=site,
                state=state,
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
        finally:
            with contextlib.suppress(Exception):
                await close_session(state, provider=provider)


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
    parser.add_argument("--sources", default="sunat,osiptel,sunat_reps")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.environ.get("PORTAL_WORKER_CONCURRENCY", DEFAULT_CONCURRENCY)),
    )
    return parser


def run(argv: Sequence[str]) -> None:
    args = build_parser().parse_args(argv)

    credential = os.environ.get("PORTAL_WORKER_CREDENTIAL", "")
    if not args.worker_api_url or not credential:
        raise SystemExit(
            "PORTAL_WORKER_API_URL and PORTAL_WORKER_CREDENTIAL are required. "
            "Issue a credential with `portal enroll-worker`."
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
