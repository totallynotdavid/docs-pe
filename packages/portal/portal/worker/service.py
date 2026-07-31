from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os

from dataclasses import dataclass
from typing import Any, cast

import httpx

from fetch.domain.types import Doc
from fetch.pipeline.breaker import CircuitBreaker
from fetch.pipeline.fetch import fetch_one
from fetch.pipeline.session import WorkerConfig, WorkerState, close_session
from fetch.proxy.dataimpulse import DataImpulseConfig, DataImpulseProvider
from fetch.proxy.geonode import GeoNodeConfig, GeoNodeProvider
from fetch.sites.registry import SITES


@dataclass(frozen=True)
class WorkerOptions:
    portal_url: str
    token: str
    worker_id: str
    sources: tuple[str, ...]


def _provider(
    name: str, values: dict[str, str]
) -> GeoNodeProvider | DataImpulseProvider:
    if name == "geonode":
        return GeoNodeProvider(
            GeoNodeConfig(
                user=values["username"],
                password=values["password"],
                host=values["host"],
                proxy_type=cast("Any", values["proxy_type"]),
                country=values["country"],
                state=values.get("state", ""),
                city=values.get("city", ""),
                asn=values.get("asn", ""),
                strict_off=False,
                lifetime=int(values["lifetime_minutes"]),
            )
        )
    if name == "dataimpulse":
        return DataImpulseProvider(
            DataImpulseConfig(
                user=values["username"],
                password=values["password"],
                country=values["country"],
                sessttl=int(values["session_minutes"]),
                host="gw.dataimpulse.com",
            )
        )
    raise RuntimeError("proveedor proxy no compatible")


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
            base_url=self.options.portal_url, headers=headers, timeout=90
        ) as client:
            while True:
                response = await client.post(
                    "/api/worker/claim", json={"sources": self.options.sources}
                )
                response.raise_for_status()
                lease = response.json()
                if lease is None:
                    await asyncio.sleep(2)
                    continue
                result = await self._execute(cast("dict[str, Any]", lease))
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

    async def _execute(self, lease: dict[str, Any]) -> dict[str, Any]:
        source = str(lease["source"])
        credential = cast("dict[str, Any]", lease["credential"])
        provider_name = str(credential["provider"])
        provider = _provider(
            provider_name, cast("dict[str, str]", credential["config"])
        )
        site = SITES[source]
        breaker = self._breakers.setdefault(
            (source, provider_name),
            CircuitBreaker(
                provider=f"{source}:{provider_name}", run_id=self.options.worker_id
            ),
        )
        state = WorkerState()
        try:
            result = await fetch_one(
                site=site,
                state=state,
                doc=Doc(str(lease["document"])),
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
        finally:
            with contextlib.suppress(Exception):
                await close_session(state, provider=provider)
        return {
            "document": str(lease["document"]),
            "source": source,
            "status": result.status.value,
            "columns": list(site.columns),
            "rows": [list(row) for row in result.rows],
            "error_code": result.error_code,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--portal-url", default=os.environ.get("PORTAL_WORKER_URL", ""))
    parser.add_argument(
        "--worker-id", default=os.environ.get("PORTAL_WORKER_ID", "poseidon-1")
    )
    parser.add_argument("--sources", default="sunat,osiptel,sunat_reps")
    args = parser.parse_args()
    token = os.environ.get("PORTAL_WORKER_BOOTSTRAP_TOKEN", "")
    if not args.portal_url or not token:
        raise SystemExit(
            "PORTAL_WORKER_URL y PORTAL_WORKER_BOOTSTRAP_TOKEN son obligatorios"
        )
    options = WorkerOptions(
        args.portal_url.rstrip("/"),
        token,
        args.worker_id,
        tuple(value.strip() for value in args.sources.split(",") if value.strip()),
    )
    asyncio.run(OutboundWorker(options).run())
