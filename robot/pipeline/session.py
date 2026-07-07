from __future__ import annotations

import asyncio
import logging
import random
import time

from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from robot.obs.events import EGRESS_IP_UNRESOLVED, SESSION_READY, STICKY_ACQUIRE
from robot.obs.logging import kv, new_session_id
from robot.proxy.egress import resolve_egress_ip
from robot.proxy.transport import build_transport


if TYPE_CHECKING:
    from robot.domain.types import Site
    from robot.proxy.base import ProxyProvider, ProxySession


logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_S = 45.0


def build_client(*, proxy_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=build_transport(proxy_url=proxy_url),
        timeout=REQUEST_TIMEOUT_S,
        headers={"User-Agent": DEFAULT_USER_AGENT},
        follow_redirects=True,
    )


@dataclass(frozen=True)
class WorkerConfig:
    session_budget: int
    wait_min_s: float
    wait_max_s: float
    ban_cooldown_s: float


@dataclass(frozen=True)
class WorkerSession:
    proxy: ProxySession
    client: httpx.AsyncClient
    session_id: str
    egress_ip: str


@dataclass
class WorkerState:
    session: WorkerSession | None = None
    cooldown_until: float = 0.0
    uses: int = 0
    last_proxy_id: str = ""


async def ensure_session(
    state: WorkerState,
    *,
    site: Site,
    provider: ProxyProvider,
    slot_id: int,
    run_id: str,
    lane_id: int,
) -> WorkerSession:
    if state.session is not None:
        return state.session

    remaining = state.cooldown_until - time.monotonic()
    if remaining > 0:
        await asyncio.sleep(remaining)

    state.session = await _open_session(
        site=site, provider=provider, slot_id=slot_id, run_id=run_id, lane_id=lane_id
    )
    state.uses = 0
    return state.session


async def after_success(
    state: WorkerState, *, provider: ProxyProvider, cfg: WorkerConfig
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
    state: WorkerState, *, provider: ProxyProvider, cooldown_s: float
) -> None:
    if state.session is not None:
        state.last_proxy_id = state.session.proxy.proxy_id
    await close_session(state, provider=provider)
    if cooldown_s > 0:
        state.cooldown_until = max(state.cooldown_until, time.monotonic() + cooldown_s)


async def close_session(state: WorkerState, *, provider: ProxyProvider) -> None:
    if state.session is None:
        return
    session = state.session
    state.session = None
    await session.client.aclose()
    await provider.release(session.proxy)


def session_ids(state: WorkerState) -> tuple[str, str]:
    if state.session is None:
        return "", state.last_proxy_id
    return state.session.session_id, state.session.proxy.proxy_id


async def _open_session(
    *, site: Site, provider: ProxyProvider, slot_id: int, run_id: str, lane_id: int
) -> WorkerSession:
    proxy = provider.new_session(slot_id=slot_id)
    session_id = new_session_id()
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
    client = build_client(proxy_url=proxy.as_http_proxy_url())
    try:
        await site.ready(client)
        egress_ip = await resolve_egress_ip(proxy)
    except BaseException:
        await client.aclose()
        await provider.release(proxy)
        raise

    if not egress_ip:
        logger.warning(
            "%s %s",
            EGRESS_IP_UNRESOLVED,
            kv(
                run_id=run_id,
                lane_id=lane_id,
                site=site.name,
                provider=provider.name,
                session_id=session_id,
                proxy_id=proxy.proxy_id,
            ),
        )
    logger.info(
        "%s %s",
        SESSION_READY,
        kv(
            run_id=run_id,
            lane_id=lane_id,
            site=site.name,
            provider=provider.name,
            session_id=session_id,
            proxy_id=proxy.proxy_id,
            egress_ip=egress_ip,
        ),
    )
    return WorkerSession(
        proxy=proxy, client=client, session_id=session_id, egress_ip=egress_ip
    )
