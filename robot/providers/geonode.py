from __future__ import annotations

import json
import time
import uuid

from dataclasses import dataclass
from ipaddress import ip_address
from os import getenv
from typing import Literal, cast

import httpx

from dotenv import load_dotenv


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
_IP_PROBE_URLS = (
    "http://ip-api.com/json",
    "https://api.ipify.org?format=json",
    "http://httpbin.org/ip",
)


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


@dataclass(frozen=True)
class ProxySessionConfig:
    proxy_id: str
    host: str
    port: str
    password: str
    username: str
    session_id: str

    def as_http_proxy_url(self) -> str:
        return f"http://{self.username}:{self.password}@{self.host}:{self.port}"


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


def new_proxy_session(config: GeoNodeConfig, *, slot_id: int) -> ProxySessionConfig:
    port = slot_port(slot_id=slot_id)
    if port > _HTTP_STICKY_PORT_MAX:
        max_slots = _HTTP_STICKY_PORT_MAX - _HTTP_STICKY_PORT_MIN + 1
        msg = f"slot_id must be <= {max_slots}"
        raise ValueError(msg)
    session_id = _new_session_id(slot_id)
    proxy_id = f"proxy-1-port-{port}"
    username = build_username(config, session_id=session_id)
    return ProxySessionConfig(
        proxy_id=proxy_id,
        host=config.host,
        port=str(port),
        password=config.password,
        username=username,
        session_id=session_id,
    )


def release_proxy_session(
    *, config: GeoNodeConfig, session_id: str, port: int, timeout_s: float = 10.0
) -> tuple[bool, int, str]:
    return _release_sticky_session(
        user=config.user,
        password=config.password,
        session_id=session_id,
        port=port,
        timeout_s=timeout_s,
    )


def resolve_egress_ip(proxy: ProxySessionConfig) -> str:
    with httpx.Client(proxy=proxy.as_http_proxy_url(), timeout=5.0) as client:
        for _ in range(3):
            for url in _IP_PROBE_URLS:
                value = _probe_ip(client, url)
                if value:
                    return value
            time.sleep(0.2)
    return ""


def _new_session_id(slot_id: int) -> str:
    return f"s{slot_id}_{uuid.uuid4().hex[:8]}"


def _probe_ip(client: httpx.Client, url: str) -> str:
    try:
        response = client.get(url)
    except httpx.HTTPError:
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


def _release_sticky_session(
    *, user: str, password: str, session_id: str, port: int, timeout_s: float
) -> tuple[bool, int, str]:
    try:
        with httpx.Client(timeout=timeout_s, auth=(user, password)) as client:
            response = client.put(
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
