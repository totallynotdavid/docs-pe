from __future__ import annotations

import asyncio

from ipaddress import ip_address
from typing import TYPE_CHECKING

import httpx

from core.domain.errors import TransientTransportError
from core.proxy.transport import build_transport


if TYPE_CHECKING:
    from core.proxy.base import ProxySession


# Public echo services, dialed through the proxy to learn the exit IP.
_IP_PROBE_URLS = (
    "http://ip-api.com/json",
    "https://api.ipify.org?format=json",
    "http://httpbin.org/ip",
)


async def resolve_egress_ip(session: ProxySession) -> str:
    async with httpx.AsyncClient(
        transport=build_transport(proxy_url=session.as_http_proxy_url()), timeout=5.0
    ) as client:
        for _ in range(3):
            for url in _IP_PROBE_URLS:
                value = await _probe_ip(client, url)
                if value:
                    return value
            await asyncio.sleep(0.2)
    return ""


async def _probe_ip(client: httpx.AsyncClient, url: str) -> str:
    try:
        response = await client.get(url)
    except TransientTransportError:
        # The egress IP is informational; try the next URL instead of aborting.
        return ""
    if response.status_code != 200:
        return ""
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return _extract_ip(payload)


def _extract_ip(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("query", "ip", "origin"):
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        candidate = value.split(",", 1)[0].strip()
        if _is_valid_ip(candidate):
            return candidate
    return ""


def _is_valid_ip(value: str) -> bool:
    try:
        ip_address(value)
    except ValueError:
        return False
    return True
