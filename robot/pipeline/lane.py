from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from typing import TYPE_CHECKING

from robot.domain.policy import MAX_ATTEMPTS, classify_exception
from robot.domain.types import LookupResult, Status
from robot.obs.events import LOOKUP_FAILED, LOOKUP_OK, WORKER_UNHANDLED_EXCEPTION
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
    from robot.pipeline.breaker import CircuitBreaker
    from robot.providers.proxy import ProxyProvider
    from robot.store.outcome_log import OutcomeLog


logger = logging.getLogger(__name__)


async def run_lane(
    *,
    queue: asyncio.Queue[RUC],
    log: OutcomeLog,
    provider: ProxyProvider,
    breaker: CircuitBreaker,
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
                breaker=breaker,
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
    breaker: CircuitBreaker,
    slot_id: int,
    run_id: str,
    lane_id: int,
    cfg: LaneConfig,
) -> LookupResult:
    attempt = 0
    while True:
        attempt += 1
        try:
            await breaker.acquire()
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
            breaker.record_success()
            # A successful lookup must never be downgraded by post-success
            # bookkeeping, so any error winding down the session is swallowed.
            with contextlib.suppress(Exception):
                await after_success(state, provider=provider, cfg=cfg)
            return result
        except Exception as exc:  # noqa: BLE001 - the lane is the fault boundary
            # Nothing may escape a lane into the TaskGroup, or one bad proxy read
            # takes down every lane. Even an unknown exception is handled here.
            decision = classify_exception(
                exc, ban_cooldown_s=provider.tuning.ban_cooldown_s
            )
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
                    error_type=type(exc).__name__,
                    error_detail=str(exc),
                ),
            )
            if decision.error_code == "unknown_error":
                logger.warning(
                    "%s %s",
                    WORKER_UNHANDLED_EXCEPTION,
                    kv(
                        run_id=run_id,
                        lane_id=lane_id,
                        ruc=ruc,
                        error_type=type(exc).__name__,
                    ),
                )
            # Every fault is environmental, so it always feeds the breaker and
            # rotates to a fresh session.
            breaker.record_failure()
            with contextlib.suppress(Exception):
                await rotate_session(
                    state, provider=provider, cooldown_s=decision.cooldown_s
                )
            if attempt >= MAX_ATTEMPTS:
                return LookupResult(
                    ruc=ruc,
                    status=Status.FAILED,
                    error_code=decision.error_code,
                    error_detail=str(exc),
                    made_healthy_contact=not breaker.is_open(),
                    http_session_id=session_id,
                    proxy_id=proxy_id,
                    attempt=attempt,
                )
