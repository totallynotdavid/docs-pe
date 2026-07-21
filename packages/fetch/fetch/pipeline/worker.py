from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from fetch.domain.types import Status
from fetch.pipeline.fetch import fetch_one
from fetch.pipeline.session import WorkerState, close_session


if TYPE_CHECKING:
    from fetch.domain.types import Doc, RunTotals, Site
    from fetch.pipeline.breaker import CircuitBreaker
    from fetch.pipeline.session import WorkerConfig
    from fetch.proxy.base import ProxyProvider
    from fetch.store.outcomes import OutcomeStore


async def run_worker(
    *,
    queue: asyncio.Queue[Doc],
    site: Site,
    store: OutcomeStore,
    provider: ProxyProvider,
    breaker: CircuitBreaker,
    slot_id: int,
    lane_id: int,
    run_id: str,
    cfg: WorkerConfig,
    totals: RunTotals,
) -> None:
    state = WorkerState()
    try:
        while True:
            try:
                doc = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            result = await fetch_one(
                site=site,
                state=state,
                doc=doc,
                provider=provider,
                breaker=breaker,
                slot_id=slot_id,
                run_id=run_id,
                lane_id=lane_id,
                cfg=cfg,
            )
            if result.status is Status.OK:
                store.record_success(result)
                totals.succeeded += 1
            elif result.status is Status.NOT_FOUND:
                store.record_not_found(result)
                totals.not_found += 1
            else:
                store.record_failure(result)
                totals.failed += 1
            totals.processed += 1
    finally:
        await close_session(state, provider=provider)
