from __future__ import annotations

import asyncio
import json
import logging
import uuid

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

import httpx

from fetch.obs.events import STICKY_RELEASE_FAILED
from fetch.obs.logging import kv
from fetch.proxy.base import (
    Field,
    ProviderSpec,
    ProviderTuning,
    ProxySession,
    country_code,
    flag,
    one_of,
    optional,
    required,
    whole_number,
)


if TYPE_CHECKING:
    from collections.abc import Mapping


logger = logging.getLogger(__name__)

ProxyType = Literal["residential", "datacenter", "mix"]

_PROXY_TYPES: tuple[str, ...] = ("residential", "datacenter", "mix")

_GATEWAY_HOST_BY_NAME: dict[str, str] = {
    "fr": "proxy.geonode.io",
    "fr_whitelist": "prod-proxy.geonode.io",
    "us": "us.proxy.geonode.io",
    "sg": "sg.proxy.geonode.io",
}

_HTTP_STICKY_PORT_MIN = 10000
_HTTP_STICKY_PORT_MAX = 10900
_HTTP_STICKY_SLOT_COUNT = _HTTP_STICKY_PORT_MAX - _HTTP_STICKY_PORT_MIN + 1

_RELEASE_URL = "https://monitor.geonode.com/sessions/release/proxies"

# GeoNode may return transient 5xx responses under gateway load.
_RELEASE_RETRIES = 3

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


_FIELDS = (
    Field("username", secret=True),
    Field("password", secret=True),
    Field("gateway", default="fr", choices=tuple(sorted(_GATEWAY_HOST_BY_NAME))),
    Field("proxy_type", default="residential", choices=_PROXY_TYPES),
    Field("country", default="PE"),
    Field("state", required=False),
    Field("city", required=False),
    Field("asn", required=False),
    Field("strict_off", required=False),
    Field("lifetime_minutes", default="10"),
)


def _normalize(raw: Mapping[str, str]) -> dict[str, str]:
    return {
        "username": required(raw, "username"),
        "password": required(raw, "password"),
        "gateway": one_of(
            raw,
            "gateway",
            tuple(sorted(_GATEWAY_HOST_BY_NAME)),
        ),
        "proxy_type": one_of(raw, "proxy_type", _PROXY_TYPES),
        "country": country_code(raw, "country"),
        "state": optional(raw, "state"),
        "city": optional(raw, "city"),
        "asn": optional(raw, "asn"),
        "strict_off": flag(raw, "strict_off"),
        "lifetime_minutes": str(
            whole_number(
                raw,
                "lifetime_minutes",
                minimum=3,
                maximum=1440,
            )
        ),
    }


def _build(values: Mapping[str, str]) -> GeoNodeProvider:
    return GeoNodeProvider(
        GeoNodeConfig(
            user=values["username"],
            password=values["password"],
            host=_GATEWAY_HOST_BY_NAME[values["gateway"]],
            proxy_type=cast("ProxyType", values["proxy_type"]),
            country=values["country"],
            state=values.get("state", ""),
            city=values.get("city", ""),
            asn=values.get("asn", ""),
            strict_off=bool(values.get("strict_off")),
            lifetime=int(values["lifetime_minutes"]),
        )
    )


GEONODE = ProviderSpec(
    name="geonode",
    fields=_FIELDS,
    tuning=_TUNING,
    normalize=_normalize,
    build=_build,
)


def build_username(config: GeoNodeConfig, *, session_id: str) -> str:
    chunks = [config.user, "session", session_id]

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
    if slot_id < 1 or slot_id > _HTTP_STICKY_SLOT_COUNT:
        msg = f"slot_id must be between 1 and {_HTTP_STICKY_SLOT_COUNT}"
        raise ValueError(msg)

    return _HTTP_STICKY_PORT_MIN + slot_id - 1


class GeoNodeProvider:
    """One sticky port per lane, with explicit release through GeoNode's API.

    A new session id in the username forces a different exit when a lane rotates.
    """

    name = "geonode"
    tuning = _TUNING

    def __init__(self, config: GeoNodeConfig) -> None:
        self._config = config

    def new_session(self, *, slot_id: int) -> ProxySession:
        port = slot_port(slot_id=slot_id)
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
    *,
    user: str,
    password: str,
    session_id: str,
    port: int,
    timeout_s: float,
) -> tuple[bool, int, str]:
    # GeoNode expects PUT {"data": [{"sessionId": ..., "port": ...}]} and a
    # 200 response with a truthy `success` field.
    try:
        async with httpx.AsyncClient(
            timeout=timeout_s,
            auth=(user, password),
        ) as client:
            response = await client.put(
                _RELEASE_URL,
                json={
                    "data": [
                        {
                            "sessionId": session_id,
                            "port": port,
                        }
                    ]
                },
            )

        if response.status_code != 200:
            return False, response.status_code, response.text[:300]

        payload = response.json()

        if not isinstance(payload, dict) or not bool(payload.get("success")):
            return False, response.status_code, json.dumps(payload)[:300]

        return True, response.status_code, ""
    except (httpx.HTTPError, ValueError) as exc:
        return False, 0, f"{type(exc).__name__}: {exc}"
