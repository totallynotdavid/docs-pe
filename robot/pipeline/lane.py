from __future__ import annotations

import asyncio
import logging
import random
import time

from dataclasses import dataclass
from typing import TYPE_CHECKING

from robot.domain.errors import RobotError
from robot.domain.policy import classify
from robot.domain.types import RUC, LaneTotals, LookupResult, Status
from robot.obs.events import (
    EGRESS_IP_UNRESOLVED,
    LOOKUP_FAILED,
    LOOKUP_OK,
    SESSION_READY,
    STICKY_ACQUIRE,
    STICKY_RELEASE_FAILED,
)
from robot.obs.logging import kv
from robot.providers.geonode import (
    new_proxy_session,
    release_proxy_session,
    resolve_egress_ip,
)
from robot.providers.osiptel import OsiptelProvider, OsiptelSession
from robot.providers.osiptel.session import build_client


if TYPE_CHECKING:
    from robot.jobs.store import JobStore
    from robot.providers.geonode import GeoNodeConfig, ProxySessionConfig


logger = logging.getLogger(__name__)

MAX_ATTEMPTS_PER_RUC = 3


@dataclass(frozen=True)
class LaneConfig:
    page_size: int
    session_budget: int
    wait_min_s: float
    wait_max_s: float
    ban_cooldown_s: float
    release_retries: int


@dataclass
class _ActiveSession:
    proxy: ProxySessionConfig
    osiptel: OsiptelSession
    egress_ip: str
    uses: int = 0


class ProxyLane:
    """One concurrent lane: a single sticky proxy session that processes RUCs.

    Single-threaded by construction. The event loop never preempts a store call
    or a session mutation mid-statement, so the lane writes results to the shared
    store directly and reads/mutates its own session state without locking. Do
    not introduce an ``await`` inside a store transaction or between reading and
    mutating ``self._active``.
    """

    def __init__(
        self,
        *,
        run_id: str,
        lane_id: int,
        slot_id: int,
        geonode: GeoNodeConfig,
        store: JobStore,
        cfg: LaneConfig,
    ) -> None:
        self._run_id = run_id
        self._lane_id = lane_id
        self._slot_id = slot_id
        self._geonode = geonode
        self._store = store
        self._cfg = cfg
        self._provider = OsiptelProvider(page_size=cfg.page_size)
        self._active: _ActiveSession | None = None
        self._last_proxy_id = ""
        self._cooldown_until = 0.0

    async def run(self, feed: asyncio.Queue[RUC | None]) -> LaneTotals:
        totals = LaneTotals()
        try:
            while True:
                ruc = await feed.get()
                if ruc is None:
                    break
                result = await self.lookup(ruc)
                if result.status is Status.OK:
                    self._store.complete_success(ruc=ruc, result=result)
                    totals.succeeded += 1
                else:
                    self._store.complete_failure(ruc=ruc, result=result)
                    totals.failed += 1
                totals.processed += 1
        finally:
            await self._close_active(cooldown_s=0.0)
        return totals

    async def lookup(self, ruc: RUC) -> LookupResult:
        attempt = 0
        while True:
            attempt += 1
            try:
                active = await self._ensure_active()
                started = time.perf_counter()
                total, carriers = await self._provider.lookup_ruc(
                    session=active.osiptel, ruc=ruc
                )
                logger.info(
                    "%s %s",
                    LOOKUP_OK,
                    kv(
                        run_id=self._run_id,
                        lane_id=self._lane_id,
                        session_id=active.osiptel.session_id,
                        proxy_id=active.proxy.proxy_id,
                        egress_ip=active.egress_ip,
                        ruc=ruc,
                        attempt=attempt,
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                        lines=total,
                        carriers=len(carriers),
                    ),
                )
                await self._after_success()
                return LookupResult(
                    ruc=ruc,
                    status=Status.OK,
                    total_lines=total,
                    carrier_counts=carriers,
                    http_session_id=active.osiptel.session_id,
                    proxy_id=active.proxy.proxy_id,
                    attempt=attempt,
                )
            except RobotError as exc:
                decision = classify(exc, ban_cooldown_s=self._cfg.ban_cooldown_s)
                session_id, proxy_id = self._active_ids()
                logger.warning(
                    "%s %s",
                    LOOKUP_FAILED,
                    kv(
                        run_id=self._run_id,
                        lane_id=self._lane_id,
                        session_id=session_id,
                        proxy_id=proxy_id,
                        ruc=ruc,
                        attempt=attempt,
                        error_code=decision.error_code,
                        error_detail=str(exc),
                    ),
                )
                if decision.rotate:
                    await self._close_active(cooldown_s=decision.cooldown_s)
                if not decision.retry or attempt >= MAX_ATTEMPTS_PER_RUC:
                    return LookupResult(
                        ruc=ruc,
                        status=Status.FAILED,
                        error_code=decision.error_code,
                        error_detail=str(exc),
                        http_session_id=session_id,
                        proxy_id=proxy_id,
                        attempt=attempt,
                    )

    async def _ensure_active(self) -> _ActiveSession:
        if self._active is not None:
            return self._active

        remaining = self._cooldown_until - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining)

        proxy = new_proxy_session(self._geonode, slot_id=self._slot_id)
        self._last_proxy_id = proxy.proxy_id
        logger.info(
            "%s %s",
            STICKY_ACQUIRE,
            kv(
                proxy_id=proxy.proxy_id,
                session_id=proxy.session_id,
                port=proxy.port,
                slot_id=self._slot_id,
            ),
        )
        osiptel = OsiptelSession(
            client=build_client(proxy_url=proxy.as_http_proxy_url())
        )
        try:
            await osiptel.wait_ready()
            egress_ip = await resolve_egress_ip(proxy)
        except BaseException:
            await osiptel.close()
            await self._release(proxy)
            raise

        if not egress_ip:
            logger.warning(
                "%s %s",
                EGRESS_IP_UNRESOLVED,
                kv(
                    run_id=self._run_id,
                    lane_id=self._lane_id,
                    session_id=osiptel.session_id,
                    proxy_id=proxy.proxy_id,
                ),
            )
        self._active = _ActiveSession(proxy=proxy, osiptel=osiptel, egress_ip=egress_ip)
        logger.info(
            "%s %s",
            SESSION_READY,
            kv(
                run_id=self._run_id,
                lane_id=self._lane_id,
                session_id=osiptel.session_id,
                proxy_id=proxy.proxy_id,
                egress_ip=egress_ip,
            ),
        )
        return self._active

    async def _after_success(self) -> None:
        if self._active is None:
            return

        self._active.uses += 1
        if self._active.uses >= self._cfg.session_budget:
            await self._close_active(cooldown_s=0.0)
            return

        wait_s = random.uniform(self._cfg.wait_min_s, self._cfg.wait_max_s)
        if wait_s > 0:
            await asyncio.sleep(wait_s)

    async def _close_active(self, *, cooldown_s: float) -> None:
        if self._active is None:
            return

        active = self._active
        self._active = None
        await active.osiptel.close()
        await self._release(active.proxy)
        if cooldown_s > 0:
            self._cooldown_until = max(
                self._cooldown_until,
                time.monotonic() + cooldown_s,
            )

    async def _release(self, proxy: ProxySessionConfig) -> None:
        last_status = 0
        last_error = ""
        for attempt in range(1, self._cfg.release_retries + 1):
            ok, status, error = await release_proxy_session(
                config=self._geonode,
                session_id=proxy.session_id,
                port=int(proxy.port),
                timeout_s=10.0,
            )
            if ok:
                return
            last_status = status
            last_error = error
            if attempt < self._cfg.release_retries:
                await asyncio.sleep(0.5 * attempt)

        logger.warning(
            "%s %s",
            STICKY_RELEASE_FAILED,
            kv(
                proxy_id=proxy.proxy_id,
                session_id=proxy.session_id,
                port=proxy.port,
                status=last_status,
                error=last_error,
                attempts=self._cfg.release_retries,
            ),
        )

    def _active_ids(self) -> tuple[str, str]:
        if self._active is None:
            return "", self._last_proxy_id
        return self._active.osiptel.session_id, self._active.proxy.proxy_id
