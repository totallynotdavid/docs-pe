from __future__ import annotations

import asyncio
import logging
import random
import time

from dataclasses import dataclass
from typing import TYPE_CHECKING

from robot.obs.events import EGRESS_IP_UNRESOLVED, SESSION_READY, STICKY_ACQUIRE
from robot.obs.logging import kv
from robot.providers.egress import resolve_egress_ip
from robot.providers.osiptel import OsiptelSession
from robot.providers.osiptel.session import build_client


if TYPE_CHECKING:
    from robot.providers.proxy import ProxyProvider, ProxySession


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LaneConfig:
    page_size: int
    session_budget: int
    wait_min_s: float
    wait_max_s: float


@dataclass(frozen=True)
class Session:
    proxy: ProxySession
    osiptel: OsiptelSession
    egress_ip: str


@dataclass
class LaneState:
    session: Session | None = None
    cooldown_until: float = 0.0
    uses: int = 0
    last_proxy_id: str = ""


async def ensure_session(
    state: LaneState,
    *,
    provider: ProxyProvider,
    slot_id: int,
    run_id: str,
    lane_id: int,
) -> Session:
    if state.session is not None:
        return state.session

    remaining = state.cooldown_until - time.monotonic()
    if remaining > 0:
        await asyncio.sleep(remaining)

    state.session = await _open_session(
        provider=provider, slot_id=slot_id, run_id=run_id, lane_id=lane_id
    )
    state.uses = 0
    return state.session


async def after_success(
    state: LaneState, *, provider: ProxyProvider, cfg: LaneConfig
) -> None:
    if state.session is None:
        return

    state.uses += 1
    if state.uses >= cfg.session_budget:
        await close_session(state, provider=provider)
        return

    wait_s = random.uniform(cfg.wait_min_s, cfg.wait_max_s)
    if wait_s > 0:
        await asyncio.sleep(wait_s)


async def rotate_session(
    state: LaneState, *, provider: ProxyProvider, cooldown_s: float
) -> None:
    if state.session is not None:
        state.last_proxy_id = state.session.proxy.proxy_id
    await close_session(state, provider=provider)
    if cooldown_s > 0:
        state.cooldown_until = max(state.cooldown_until, time.monotonic() + cooldown_s)


async def close_session(state: LaneState, *, provider: ProxyProvider) -> None:
    if state.session is None:
        return
    session = state.session
    state.session = None
    await session.osiptel.close()
    await provider.release(session.proxy)


def session_ids(state: LaneState) -> tuple[str, str]:
    if state.session is None:
        return "", state.last_proxy_id
    return state.session.osiptel.session_id, state.session.proxy.proxy_id


async def _open_session(
    *, provider: ProxyProvider, slot_id: int, run_id: str, lane_id: int
) -> Session:
    proxy = provider.new_session(slot_id=slot_id)
    logger.info(
        "%s %s",
        STICKY_ACQUIRE,
        kv(
            provider=provider.name,
            proxy_id=proxy.proxy_id,
            session_id=proxy.session_id,
            port=proxy.port,
            slot_id=slot_id,
        ),
    )
    osiptel = OsiptelSession(client=build_client(proxy_url=proxy.as_http_proxy_url()))
    try:
        await osiptel.wait_ready()
        egress_ip = await resolve_egress_ip(proxy)
    except BaseException:
        await osiptel.close()
        await provider.release(proxy)
        raise

    if not egress_ip:
        logger.warning(
            "%s %s",
            EGRESS_IP_UNRESOLVED,
            kv(
                run_id=run_id,
                lane_id=lane_id,
                provider=provider.name,
                session_id=osiptel.session_id,
                proxy_id=proxy.proxy_id,
            ),
        )
    logger.info(
        "%s %s",
        SESSION_READY,
        kv(
            run_id=run_id,
            lane_id=lane_id,
            provider=provider.name,
            session_id=osiptel.session_id,
            proxy_id=proxy.proxy_id,
            egress_ip=egress_ip,
        ),
    )
    return Session(proxy=proxy, osiptel=osiptel, egress_ip=egress_ip)
