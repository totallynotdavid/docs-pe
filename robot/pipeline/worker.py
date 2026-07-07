from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from robot.domain.types import Status
from robot.pipeline.fetch import fetch_one
from robot.pipeline.session import WorkerState, close_session


if TYPE_CHECKING:
    from robot.domain.types import RUC, RunTotals, Site
    from robot.pipeline.breaker import CircuitBreaker
    from robot.pipeline.session import WorkerConfig
    from robot.proxy.base import ProxyProvider
    from robot.store.outcomes import OutcomeStore


async def run_worker(
    *,
    queue: asyncio.Queue[RUC],
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
                ruc = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            result = await fetch_one(
                site=site,
                state=state,
                ruc=ruc,
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
            else:
                store.record_failure(result)
                totals.failed += 1
            totals.processed += 1
    finally:
        await close_session(state, provider=provider)
