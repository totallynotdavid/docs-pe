from __future__ import annotations

import contextlib
import logging
import time

from typing import TYPE_CHECKING

from fetch.domain.errors import ProviderSchemaError, RucNotFoundError
from fetch.domain.policy import MAX_ATTEMPTS, classify_exception
from fetch.domain.types import Result, Status
from fetch.obs.events import (
    LOOKUP_FAILED,
    LOOKUP_NOT_FOUND,
    LOOKUP_OK,
    WORKER_UNHANDLED_EXCEPTION,
)
from fetch.obs.logging import kv
from fetch.pipeline.session import (
    after_success,
    ensure_session,
    rotate_session,
    session_ids,
)


if TYPE_CHECKING:
    from fetch.domain.types import Doc, Row, Site
    from fetch.pipeline.breaker import CircuitBreaker
    from fetch.pipeline.session import WorkerConfig, WorkerState
    from fetch.proxy.base import ProxyProvider


logger = logging.getLogger(__name__)


async def fetch_one(
    *,
    site: Site,
    state: WorkerState,
    doc: Doc,
    provider: ProxyProvider,
    breaker: CircuitBreaker,
    slot_id: int,
    run_id: str,
    lane_id: int,
    cfg: WorkerConfig,
) -> Result:
    attempt = 0

    while True:
        attempt += 1

        try:
            await breaker.acquire()

            session = await ensure_session(
                state,
                site=site,
                provider=provider,
                slot_id=slot_id,
                run_id=run_id,
                lane_id=lane_id,
            )

            lookup_started = time.perf_counter()
            rows = await site.lookup(session.client, doc)
            _enforce_allows_empty(site, rows)

            logger.info(
                "%s %s",
                LOOKUP_OK,
                kv(
                    run_id=run_id,
                    lane_id=lane_id,
                    site=site.name,
                    provider=provider.name,
                    session_id=session.session_id,
                    proxy_id=session.proxy.proxy_id,
                    egress_ip=session.egress_ip,
                    doc=doc,
                    attempt=attempt,
                    elapsed_ms=int((time.perf_counter() - lookup_started) * 1000),
                    rows=len(rows),
                ),
            )

            result = Result(
                doc=doc,
                site=site.name,
                status=Status.OK,
                rows=rows,
                http_session_id=session.session_id,
                proxy_id=session.proxy.proxy_id,
                provider=provider.name,
                attempt=attempt,
            )

            breaker.record_success()

            # Post-success cleanup must not invalidate a completed lookup.
            with contextlib.suppress(Exception):
                await after_success(state, provider=provider, cfg=cfg)

            return result

        except RucNotFoundError:
            logger.info(
                "%s %s",
                LOOKUP_NOT_FOUND,
                kv(
                    run_id=run_id,
                    lane_id=lane_id,
                    site=site.name,
                    provider=provider.name,
                    session_id=session.session_id,
                    proxy_id=session.proxy.proxy_id,
                    doc=doc,
                    attempt=attempt,
                ),
            )

            result = Result(
                doc=doc,
                site=site.name,
                status=Status.NOT_FOUND,
                http_session_id=session.session_id,
                proxy_id=session.proxy.proxy_id,
                provider=provider.name,
                attempt=attempt,
            )

            breaker.record_success()

            with contextlib.suppress(Exception):
                await after_success(state, provider=provider, cfg=cfg)

            return result

        except Exception as exc:  # ruff: ignore[blind-except]
            # Worker failures must not escape into the TaskGroup.
            decision = classify_exception(
                exc,
                ban_cooldown_s=cfg.ban_cooldown_s,
            )
            session_id, proxy_id = session_ids(state)

            logger.warning(
                "%s %s",
                LOOKUP_FAILED,
                kv(
                    run_id=run_id,
                    lane_id=lane_id,
                    site=site.name,
                    provider=provider.name,
                    session_id=session_id,
                    proxy_id=proxy_id,
                    doc=doc,
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
                        site=site.name,
                        doc=doc,
                        error_type=type(exc).__name__,
                    ),
                )

            breaker.record_failure()

            with contextlib.suppress(Exception):
                await rotate_session(
                    state,
                    provider=provider,
                    cooldown_s=decision.cooldown_s,
                )

            if attempt >= MAX_ATTEMPTS:
                return Result(
                    doc=doc,
                    site=site.name,
                    status=Status.FAILED,
                    error_code=decision.error_code,
                    error_detail=str(exc),
                    made_healthy_contact=not breaker.is_open(),
                    http_session_id=session_id,
                    proxy_id=proxy_id,
                    provider=provider.name,
                    attempt=attempt,
                )


def _enforce_allows_empty(site: Site, rows: tuple[Row, ...]) -> None:
    if rows or site.allows_empty:
        return

    msg = f"{site.name} returned no rows but disallows empty results"
    raise ProviderSchemaError(msg)
