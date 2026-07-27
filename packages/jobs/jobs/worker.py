from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import uuid

from dataclasses import dataclass
from typing import Any, cast

import httpx

from fetch.domain.types import Doc, Status
from fetch.pipeline.breaker import CircuitBreaker
from fetch.pipeline.fetch import fetch_one
from fetch.pipeline.session import WorkerConfig, WorkerState, close_session
from fetch.proxy.dataimpulse import DataImpulseConfig, DataImpulseProvider
from fetch.proxy.geonode import GeoNodeConfig, GeoNodeProvider
from fetch.sites.registry import SITES


@dataclass(frozen=True)
class WorkerOptions:
    base_url: str
    worker_id: str
    token: str
    sources: list[str]
    capacity: int
    lease_seconds: int


class FetchAdapter:
    """Executes stable fetch adapters while keeping the service checkpoint authoritative.

    The source's warm-up, sticky sessions, retry taxonomy, and local breaker are
    reused exactly through ``fetch_one``. This class owns no local outcome store.
    """

    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id
        self._breakers: dict[tuple[str, str], CircuitBreaker] = {}

    async def execute(
        self, lease: dict[str, Any], credential: dict[str, Any]
    ) -> dict[str, Any]:
        source = str(lease["source"])
        provider_name = str(credential["provider"])
        site = SITES[source]
        provider = _provider(
            provider_name, cast("dict[str, Any]", credential["config"])
        )
        key = (source, provider_name)
        breaker = self._breakers.setdefault(
            key,
            CircuitBreaker(provider=f"{source}:{provider_name}", run_id=self.worker_id),
        )
        state = WorkerState()
        config = WorkerConfig(
            session_budget=site.tuning.session_budget,
            wait_min_s=0,
            wait_max_s=0,
            ban_cooldown_s=provider.tuning.ban_cooldown_s,
        )
        try:
            result = await fetch_one(
                site=site,
                state=state,
                doc=Doc(str(lease["document"])),
                provider=provider,
                breaker=breaker,
                slot_id=1,
                run_id=self.worker_id,
                lane_id=1,
                cfg=config,
            )
        finally:
            await close_session(state, provider=provider)
        if result.status is Status.OK:
            return {
                "outcome": "succeeded",
                "payload": {"rows": [list(row) for row in result.rows]},
                "healthy_contact_delta": result.attempt,
            }
        if result.status is Status.NOT_FOUND:
            return {
                "outcome": "not_found",
                "payload": {},
                "healthy_contact_delta": result.attempt,
            }
        return {
            "outcome": "retryable",
            "error_code": result.error_code,
            "healthy_contact_delta": result.attempt
            if result.made_healthy_contact
            else 0,
        }


class OutboundWorker:
    def __init__(self, options: WorkerOptions) -> None:
        self.options = options
        self.executor = FetchAdapter(options.worker_id)

    async def run(self) -> None:
        async with httpx.AsyncClient(
            base_url=self.options.base_url, timeout=70
        ) as client:
            await self._post(client, "/api/worker/register", self._identity())
            while True:
                leases = await self._post(
                    client,
                    "/api/worker/claim",
                    {
                        **self._identity(),
                        "max_items": self.options.capacity,
                        "lease_seconds": self.options.lease_seconds,
                    },
                )
                if not leases:
                    await asyncio.sleep(1)
                    continue
                async with asyncio.TaskGroup() as tasks:
                    for lease in leases:
                        tasks.create_task(
                            self._run_lease(client, cast("dict[str, Any]", lease))
                        )

    async def _run_lease(
        self, client: httpx.AsyncClient, lease: dict[str, Any]
    ) -> None:
        lease_id = str(lease["lease_id"])
        credential = await self._post(
            client, f"/api/worker/leases/{lease_id}/credential", self._identity()
        )
        task = asyncio.create_task(
            self.executor.execute(lease, cast("dict[str, Any]", credential))
        )
        try:
            while not task.done():
                done, _ = await asyncio.wait({task}, timeout=1)
                if done:
                    break
                cancelled = await self._post(
                    client,
                    f"/api/worker/leases/{lease_id}/cancelled",
                    self._identity(),
                )
                if cancelled["cancelled"]:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                    await self._ack_cancelled(client, lease)
                    return
                await self._post(
                    client,
                    f"/api/worker/leases/{lease_id}/renew",
                    {
                        **self._identity(),
                        "fence": lease["fence"],
                        "lease_seconds": self.options.lease_seconds,
                    },
                )
            outcome = await task
            await self._post(
                client,
                "/api/worker/checkpoints",
                {
                    **self._identity(),
                    "lease_id": lease_id,
                    "work_item_id": lease["work_item_id"],
                    "fence": lease["fence"],
                    "version": lease["version"],
                    "attempt_id": str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"{lease_id}:{lease['work_item_id']}:1",
                        )
                    ),
                    "sequence": 1,
                    **outcome,
                },
            )
        except httpx.HTTPStatusError:
            # A cancellation or stale-fence response has already made the server
            # authoritative. Do not retry it locally or write a local outcome file.
            return

    async def _ack_cancelled(
        self, client: httpx.AsyncClient, lease: dict[str, Any]
    ) -> None:
        with contextlib.suppress(httpx.HTTPStatusError):
            await self._post(
                client,
                "/api/worker/checkpoints",
                {
                    **self._identity(),
                    "lease_id": lease["lease_id"],
                    "work_item_id": lease["work_item_id"],
                    "fence": lease["fence"],
                    "version": lease["version"],
                    "attempt_id": str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"{lease['lease_id']}:{lease['work_item_id']}:cancel",
                        )
                    ),
                    "sequence": 1,
                    "outcome": "cancelled",
                },
            )

    def _identity(self) -> dict[str, Any]:
        return {
            "worker_id": self.options.worker_id,
            "token": self.options.token,
            "sources": self.options.sources,
            "capacity": self.options.capacity,
        }

    @staticmethod
    async def _post(
        client: httpx.AsyncClient, path: str, payload: dict[str, Any]
    ) -> Any:
        response = await client.post(path, json=payload)
        response.raise_for_status()
        if response.status_code == 204:
            return {}
        return response.json()


def _provider(
    name: str, config: dict[str, Any]
) -> GeoNodeProvider | DataImpulseProvider:
    if name == "geonode":
        return GeoNodeProvider(
            GeoNodeConfig(
                user=str(config["user"]),
                password=str(config["password"]),
                host=str(config.get("host", "proxy.geonode.io")),
                proxy_type=cast("Any", config.get("proxy_type", "residential")),
                country=str(config.get("country", "PE")),
                state=str(config.get("state", "")),
                city=str(config.get("city", "")),
                asn=str(config.get("asn", "")),
                strict_off=bool(config.get("strict_off")),
                lifetime=int(config.get("lifetime", 10)),
            )
        )
    if name == "dataimpulse":
        return DataImpulseProvider(
            DataImpulseConfig(
                user=str(config["user"]),
                password=str(config["password"]),
                country=str(config.get("country", "pe")),
                sessttl=int(config.get("sessttl", 3)),
                host=str(config.get("host", "gw.dataimpulse.com")),
            )
        )
    raise RuntimeError("unsupported proxy provider")


def parse_args(argv: list[str] | None = None) -> WorkerOptions:
    parser = argparse.ArgumentParser(prog="osiptel-jobs-worker")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--token-env", default="JOBS_WORKER_BOOTSTRAP_TOKEN")
    parser.add_argument("--sources", default="osiptel,sunat,sunat_reps")
    parser.add_argument("--capacity", type=int, default=1)
    parser.add_argument("--lease-seconds", type=int, default=60)
    ns = parser.parse_args(argv)
    token = os.environ.get(ns.token_env, "")
    if not token:
        parser.error(f"environment variable {ns.token_env} must be set")
    return WorkerOptions(
        base_url=ns.base_url.rstrip("/"),
        worker_id=ns.worker_id,
        token=token,
        sources=[item.strip() for item in ns.sources.split(",") if item.strip()],
        capacity=ns.capacity,
        lease_seconds=ns.lease_seconds,
    )


def main() -> None:
    if os.environ.get("JOBS_ALLOW_LIVE_LOOKUPS", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        raise RuntimeError(
            "set JOBS_ALLOW_LIVE_LOOKUPS=true to run real source lookups"
        )
    asyncio.run(OutboundWorker(parse_args()).run())
