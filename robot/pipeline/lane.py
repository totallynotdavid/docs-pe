from __future__ import annotations

import asyncio
import logging
import time

from typing import TYPE_CHECKING

from robot.domain.errors import RobotError
from robot.domain.policy import MAX_ATTEMPTS, classify
from robot.domain.types import LookupResult, Status
from robot.obs.events import LOOKUP_FAILED, LOOKUP_OK
from robot.obs.logging import kv
from robot.pipeline.session import (
    LaneConfig,
    LaneState,
    after_success,
    close_session,
    ensure_session,
    rotate_session,
    session_ids,
)
from robot.providers.osiptel import OsiptelProvider


if TYPE_CHECKING:
    from robot.domain.types import RUC, RunTotals
    from robot.providers.proxy import ProxyProvider
    from robot.store.outcome_log import OutcomeLog


logger = logging.getLogger(__name__)


async def run_lane(
    *,
    queue: asyncio.Queue[RUC],
    log: OutcomeLog,
    provider: ProxyProvider,
    slot_id: int,
    lane_id: int,
    run_id: str,
    cfg: LaneConfig,
    totals: RunTotals,
) -> None:
    api = OsiptelProvider(page_size=cfg.page_size)
    state = LaneState()
    try:
        while True:
            try:
                ruc = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            result = await _lookup(
                api=api,
                state=state,
                ruc=ruc,
                provider=provider,
                slot_id=slot_id,
                run_id=run_id,
                lane_id=lane_id,
                cfg=cfg,
            )
            if result.status is Status.OK:
                log.record_success(result)
                totals.succeeded += 1
            else:
                log.record_failure(result)
                totals.failed += 1
            totals.processed += 1
    finally:
        await close_session(state, provider=provider)


async def _lookup(
    *,
    api: OsiptelProvider,
    state: LaneState,
    ruc: RUC,
    provider: ProxyProvider,
    slot_id: int,
    run_id: str,
    lane_id: int,
    cfg: LaneConfig,
) -> LookupResult:
    attempt = 0
    while True:
        attempt += 1
        try:
            session = await ensure_session(
                state,
                provider=provider,
                slot_id=slot_id,
                run_id=run_id,
                lane_id=lane_id,
            )
            started = time.perf_counter()
            total, carriers = await api.lookup_ruc(session=session.osiptel, ruc=ruc)
            logger.info(
                "%s %s",
                LOOKUP_OK,
                kv(
                    run_id=run_id,
                    lane_id=lane_id,
                    provider=provider.name,
                    session_id=session.osiptel.session_id,
                    proxy_id=session.proxy.proxy_id,
                    egress_ip=session.egress_ip,
                    ruc=ruc,
                    attempt=attempt,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                    lines=total,
                    carriers=len(carriers),
                ),
            )
            result = LookupResult(
                ruc=ruc,
                status=Status.OK,
                total_lines=total,
                carrier_counts=carriers,
                http_session_id=session.osiptel.session_id,
                proxy_id=session.proxy.proxy_id,
                attempt=attempt,
            )
            await after_success(state, provider=provider, cfg=cfg)
            return result
        except RobotError as exc:
            decision = classify(exc, ban_cooldown_s=provider.tuning.ban_cooldown_s)
            session_id, proxy_id = session_ids(state)
            logger.warning(
                "%s %s",
                LOOKUP_FAILED,
                kv(
                    run_id=run_id,
                    lane_id=lane_id,
                    provider=provider.name,
                    session_id=session_id,
                    proxy_id=proxy_id,
                    ruc=ruc,
                    attempt=attempt,
                    error_code=decision.error_code,
                    error_detail=str(exc),
                ),
            )
            if decision.rotate:
                await rotate_session(
                    state, provider=provider, cooldown_s=decision.cooldown_s
                )
            if not decision.retry or attempt >= MAX_ATTEMPTS:
                return LookupResult(
                    ruc=ruc,
                    status=Status.FAILED,
                    error_code=decision.error_code,
                    error_detail=str(exc),
                    http_session_id=session_id,
                    proxy_id=proxy_id,
                    attempt=attempt,
                )
