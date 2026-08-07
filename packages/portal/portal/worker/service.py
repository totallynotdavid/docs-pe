from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os

from dataclasses import dataclass
from typing import TypedDict, cast

import httpx

from fetch.domain.types import Doc
from fetch.pipeline.breaker import CircuitBreaker
from fetch.pipeline.fetch import fetch_one
from fetch.pipeline.session import WorkerConfig, WorkerState, close_session
from fetch.proxy.registry import provider_from_values
from fetch.sites.registry import SITES


DEFAULT_CONCURRENCY = 4


class CredentialLease(TypedDict):
    provider: str
    config: dict[str, str]


class WorkLease(TypedDict):
    item_id: str
    fence: int
    document: str
    source: str
    credential: CredentialLease


@dataclass(frozen=True)
class WorkerOptions:
    portal_url: str
    token: str
    worker_id: str
    sources: tuple[str, ...]
    concurrency: int = DEFAULT_CONCURRENCY


class OutboundWorker:
    def __init__(self, options: WorkerOptions) -> None:
        self.options = options
        self._breakers: dict[tuple[str, str], CircuitBreaker] = {}

    async def run(self) -> None:
        headers = {
            "Authorization": f"Bearer {self.options.token}",
            "X-Portal-Worker": self.options.worker_id,
        }

        async with httpx.AsyncClient(
            base_url=self.options.portal_url,
            headers=headers,
            timeout=90,
        ) as client:
            await asyncio.gather(
                *(self._loop(client) for _ in range(self.options.concurrency))
            )

    async def _loop(self, client: httpx.AsyncClient) -> None:
        while True:
            response = await client.post(
                "/api/worker/claim",
                json={"sources": self.options.sources},
            )
            response.raise_for_status()

            raw_lease = response.json()
            if raw_lease is None:
                await asyncio.sleep(2)
                continue

            lease = cast("WorkLease", raw_lease)
            result = await self._execute(lease)
            content = base64.b64encode(
                json.dumps(result, separators=(",", ":")).encode()
            ).decode()

            response = await client.post(
                "/api/worker/publish",
                json={
                    "item_id": lease["item_id"],
                    "fence": lease["fence"],
                    "content": content,
                },
            )
            response.raise_for_status()

    async def _execute(self, lease: WorkLease) -> dict[str, object]:
        source = lease["source"]
        credential = lease["credential"]
        provider_name = credential["provider"]

        provider = provider_from_values(provider_name, credential["config"])
        site = SITES[source]

        breaker = self._breakers.setdefault(
            (source, provider_name),
            CircuitBreaker(
                provider=f"{source}:{provider_name}",
                run_id=self.options.worker_id,
            ),
        )

        state = WorkerState()

        try:
            result = await fetch_one(
                site=site,
                state=state,
                doc=Doc(lease["document"]),
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
                "document": lease["document"],
                "source": source,
                "status": result.status.value,
                "columns": list(site.columns),
                "rows": [list(row) for row in result.rows],
                "error_code": result.error_code,
            }
        finally:
            with contextlib.suppress(Exception):
                await close_session(state, provider=provider)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--portal-url",
        default=os.environ.get("PORTAL_WORKER_URL", ""),
    )
    parser.add_argument(
        "--worker-id",
        default=os.environ.get("PORTAL_WORKER_ID", "poseidon-1"),
    )
    parser.add_argument(
        "--sources",
        default="sunat,osiptel,sunat_reps",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(
            os.environ.get(
                "PORTAL_WORKER_CONCURRENCY",
                DEFAULT_CONCURRENCY,
            )
        ),
    )
    args = parser.parse_args()

    token = os.environ.get("PORTAL_WORKER_BOOTSTRAP_TOKEN", "")
    if not args.portal_url or not token:
        raise SystemExit(
            "PORTAL_WORKER_URL and PORTAL_WORKER_BOOTSTRAP_TOKEN are required"
        )

    sources = tuple(value.strip() for value in args.sources.split(",") if value.strip())
    if not sources:
        raise SystemExit("at least one source is required")

    if args.concurrency < 1:
        raise SystemExit("concurrency must be at least 1")

    options = WorkerOptions(
        portal_url=args.portal_url.rstrip("/"),
        token=token,
        worker_id=args.worker_id,
        sources=sources,
        concurrency=args.concurrency,
    )

    asyncio.run(OutboundWorker(options).run())
