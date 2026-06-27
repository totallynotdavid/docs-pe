from __future__ import annotations

import asyncio
import json
import logging
import uuid

from dataclasses import dataclass
from os import getenv
from typing import Literal, cast

import httpx

from dotenv import load_dotenv

from robot.obs.events import STICKY_RELEASE_FAILED
from robot.obs.logging import kv
from robot.providers.proxy import ProviderTuning, ProxySession


logger = logging.getLogger(__name__)

ProxyType = Literal["residential", "datacenter", "mix"]

_GATEWAY_HOST_BY_NAME: dict[str, str] = {
    "fr": "proxy.geonode.io",
    "fr_whitelist": "prod-proxy.geonode.io",
    "us": "us.proxy.geonode.io",
    "sg": "sg.proxy.geonode.io",
}
_HTTP_STICKY_PORT_MIN = 10000
_HTTP_STICKY_PORT_MAX = 10900
_RELEASE_URL = "https://monitor.geonode.com/sessions/release/proxies"
_RELEASE_RETRIES = 3

# GeoNode's measured default balances throughput and retryable proxy failures.
# ban_cooldown_s parks a banned sticky IP before the next acquisition.
_TUNING = ProviderTuning(workers=15, ban_cooldown_s=30.0)


@dataclass(frozen=True)
class GeoNodeConfig:
    user: str
    password: str
    host: str
    proxy_type: ProxyType
    country: str
    state: str
    city: str
    asn: str
    strict_off: bool
    lifetime: int


def load_geonode_config(*, env_file: str) -> GeoNodeConfig:
    load_dotenv(env_file, override=False)

    user = getenv("GEONODE_USER", "")
    password = getenv("GEONODE_PASS", "")
    gateway = getenv("GEONODE_GATEWAY", "fr")
    proxy_type_raw = getenv("GEONODE_TYPE", "residential")
    country = getenv("GEONODE_COUNTRY", "")
    state = getenv("GEONODE_STATE", "")
    city = getenv("GEONODE_CITY", "")
    asn = getenv("GEONODE_ASN", "")
    strict_off = getenv("GEONODE_STRICT_OFF", "").lower() in {"1", "true", "yes"}
    lifetime_raw = getenv("GEONODE_LIFETIME", "").strip()
    lifetime = int(lifetime_raw) if lifetime_raw else 10

    if not user or not password:
        msg = "missing GEONODE_USER or GEONODE_PASS"
        raise RuntimeError(msg)
    if not country:
        # OSIPTEL's WAF blocks foreign exits, so an unset country silently routes
        # through the wrong region and fails every lookup. Fail loudly instead.
        msg = "GEONODE_COUNTRY must be set (OSIPTEL requires Peru exits, e.g. PE)"
        raise RuntimeError(msg)
    if gateway not in _GATEWAY_HOST_BY_NAME:
        msg = "GEONODE_GATEWAY must be one of " + "|".join(
            sorted(_GATEWAY_HOST_BY_NAME)
        )
        raise RuntimeError(msg)
    if proxy_type_raw not in {"residential", "datacenter", "mix"}:
        msg = "GEONODE_TYPE must be one of residential|datacenter|mix"
        raise RuntimeError(msg)
    if lifetime < 3 or lifetime > 1440:
        msg = "GEONODE_LIFETIME must be between 3 and 1440 minutes"
        raise RuntimeError(msg)

    proxy_type = cast("ProxyType", proxy_type_raw)
    return GeoNodeConfig(
        user=user,
        password=password,
        host=_GATEWAY_HOST_BY_NAME[gateway],
        proxy_type=proxy_type,
        country=country,
        state=state,
        city=city,
        asn=asn,
        strict_off=strict_off,
        lifetime=lifetime,
    )


def build_username(config: GeoNodeConfig, *, session_id: str) -> str:
    chunks: list[str] = [config.user, "session", session_id]

    if config.proxy_type:
        chunks.extend(["type", config.proxy_type])
    if config.country:
        chunks.extend(["country", config.country])
    if config.state:
        chunks.extend(["state", config.state])
    if config.city:
        chunks.extend(["city", config.city])
    if config.asn:
        chunks.extend(["asn", config.asn])
    if config.strict_off:
        chunks.extend(["strict", "off"])
    if config.lifetime:
        chunks.extend(["lifetime", str(config.lifetime)])

    return "-".join(chunks)


def slot_port(*, slot_id: int) -> int:
    if slot_id < 1:
        msg = "slot_id must be >= 1"
        raise ValueError(msg)
    return _HTTP_STICKY_PORT_MIN + slot_id - 1


class GeoNodeProvider:
    """GeoNode sticky sessions: one dedicated port per lane, released via API.

    Stickiness comes from the per-slot port; a fresh random session id in the
    username forces a new exit IP each time a lane rotates after a ban.
    """

    name = "geonode"
    tuning = _TUNING

    def __init__(self, config: GeoNodeConfig) -> None:
        self._config = config

    def new_session(self, *, slot_id: int) -> ProxySession:
        port = slot_port(slot_id=slot_id)
        if port > _HTTP_STICKY_PORT_MAX:
            max_slots = _HTTP_STICKY_PORT_MAX - _HTTP_STICKY_PORT_MIN + 1
            msg = f"slot_id must be <= {max_slots}"
            raise ValueError(msg)
        session_id = _new_session_id(slot_id)
        username = build_username(self._config, session_id=session_id)
        return ProxySession(
            proxy_id=f"proxy-1-port-{port}",
            host=self._config.host,
            port=str(port),
            password=self._config.password,
            username=username,
            session_id=session_id,
        )

    async def release(self, session: ProxySession) -> None:
        last_status = 0
        last_error = ""
        for attempt in range(1, _RELEASE_RETRIES + 1):
            ok, status, error = await _release_sticky_session(
                user=self._config.user,
                password=self._config.password,
                session_id=session.session_id,
                port=int(session.port),
                timeout_s=10.0,
            )
            if ok:
                return
            last_status = status
            last_error = error
            if attempt < _RELEASE_RETRIES:
                await asyncio.sleep(0.5 * attempt)

        logger.warning(
            "%s %s",
            STICKY_RELEASE_FAILED,
            kv(
                provider=self.name,
                proxy_id=session.proxy_id,
                session_id=session.session_id,
                port=session.port,
                status=last_status,
                error=last_error,
                attempts=_RELEASE_RETRIES,
            ),
        )


def _new_session_id(slot_id: int) -> str:
    return f"s{slot_id}_{uuid.uuid4().hex[:8]}"


async def _release_sticky_session(
    *, user: str, password: str, session_id: str, port: int, timeout_s: float
) -> tuple[bool, int, str]:
    try:
        async with httpx.AsyncClient(
            timeout=timeout_s, auth=(user, password)
        ) as client:
            response = await client.put(
                _RELEASE_URL,
                json={"data": [{"sessionId": session_id, "port": port}]},
            )
        if response.status_code != 200:
            return False, response.status_code, response.text[:300]
        payload = response.json()
        if not isinstance(payload, dict) or not bool(payload.get("success")):
            return False, response.status_code, json.dumps(payload)[:300]
        return True, response.status_code, ""
    except (httpx.HTTPError, ValueError) as exc:
        return False, 0, f"{type(exc).__name__}: {exc}"
