from __future__ import annotations

import uuid

from dataclasses import dataclass
from os import getenv
from typing import Protocol

from dotenv import load_dotenv


@dataclass(frozen=True)
class ProxyEndpoint:
    """Credentials for one upstream proxy exit. Never log the password."""

    host: str
    port: str
    username: str
    password: str


class ProxyProvider(Protocol):
    name: str

    def new_endpoint(self) -> ProxyEndpoint: ...


_GEONODE_GATEWAY_HOST = {
    "fr": "proxy.geonode.io",
    "fr_whitelist": "prod-proxy.geonode.io",
    "us": "us.proxy.geonode.io",
    "sg": "sg.proxy.geonode.io",
}
# Browser drives one session at a time, so it only ever needs the first sticky port.
_GEONODE_STICKY_PORT = "10000"

_DATAIMPULSE_HOST = "gw.dataimpulse.com"
_DATAIMPULSE_PORT = "823"

_KNOWN_PROVIDERS = ("geonode", "dataimpulse")


def _new_session_id() -> str:
    return f"b_{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class _GeoNodeConfig:
    user: str
    password: str
    host: str
    proxy_type: str
    country: str
    lifetime: int


class GeoNodeProvider:
    name = "geonode"

    def __init__(self, config: _GeoNodeConfig) -> None:
        self._config = config

    def new_endpoint(self) -> ProxyEndpoint:
        config = self._config
        username = (
            f"{config.user}-session-{_new_session_id()}"
            f"-type-{config.proxy_type}"
            f"-country-{config.country}"
            f"-lifetime-{config.lifetime}"
        )
        return ProxyEndpoint(
            host=config.host,
            port=_GEONODE_STICKY_PORT,
            username=username,
            password=config.password,
        )


@dataclass(frozen=True)
class _DataImpulseConfig:
    user: str
    password: str
    country: str
    sessttl: int


class DataImpulseProvider:
    name = "dataimpulse"

    def __init__(self, config: _DataImpulseConfig) -> None:
        self._config = config

    def new_endpoint(self) -> ProxyEndpoint:
        config = self._config
        username = (
            f"{config.user}__cr.{config.country}"
            f";sessid.{_new_session_id()};sessttl.{config.sessttl}"
        )
        return ProxyEndpoint(
            host=_DATAIMPULSE_HOST,
            port=_DATAIMPULSE_PORT,
            username=username,
            password=config.password,
        )


def load_proxy_provider(*, env_file: str) -> ProxyProvider:
    """Build the first provider listed in PROXY_PROVIDER, or fail fast."""
    load_dotenv(env_file, override=False)
    raw = getenv("PROXY_PROVIDER", "").strip()
    if not raw:
        msg = "PROXY_PROVIDER must be set (geonode or dataimpulse), or pass --no-proxy"
        raise RuntimeError(msg)

    name = next(
        (chunk.strip().lower() for chunk in raw.split(",") if chunk.strip()), ""
    )
    if name not in _KNOWN_PROVIDERS:
        allowed = "|".join(_KNOWN_PROVIDERS)
        msg = f"PROXY_PROVIDER entry {name!r} is not one of {allowed}"
        raise RuntimeError(msg)

    if name == "geonode":
        return GeoNodeProvider(_load_geonode_config())
    return DataImpulseProvider(_load_dataimpulse_config())


def _whole_number(name: str, *, default: int) -> int:
    raw = getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        msg = f"{name} must be a whole number"
        raise RuntimeError(msg) from None


def _load_geonode_config() -> _GeoNodeConfig:
    user = getenv("GEONODE_USER", "")
    password = getenv("GEONODE_PASS", "")
    gateway = getenv("GEONODE_GATEWAY", "fr")
    proxy_type = getenv("GEONODE_TYPE", "residential")
    country = getenv("GEONODE_COUNTRY", "")
    lifetime = _whole_number("GEONODE_LIFETIME", default=10)

    if not user or not password:
        msg = "missing GEONODE_USER or GEONODE_PASS"
        raise RuntimeError(msg)
    if not country:
        # An unset country silently routes through the wrong region, which the
        # Peru sites geo-gate or geo-score. Fail loudly instead.
        msg = "GEONODE_COUNTRY must be set (Peru exits, e.g. PE)"
        raise RuntimeError(msg)
    if gateway not in _GEONODE_GATEWAY_HOST:
        allowed = "|".join(sorted(_GEONODE_GATEWAY_HOST))
        msg = f"GEONODE_GATEWAY must be one of {allowed}"
        raise RuntimeError(msg)
    if proxy_type not in {"residential", "datacenter", "mix"}:
        msg = "GEONODE_TYPE must be one of residential|datacenter|mix"
        raise RuntimeError(msg)
    if lifetime < 3 or lifetime > 1440:
        msg = "GEONODE_LIFETIME must be between 3 and 1440 minutes"
        raise RuntimeError(msg)

    return _GeoNodeConfig(
        user=user,
        password=password,
        host=_GEONODE_GATEWAY_HOST[gateway],
        proxy_type=proxy_type,
        country=country,
        lifetime=lifetime,
    )


def _load_dataimpulse_config() -> _DataImpulseConfig:
    user = getenv("DATAIMPULSE_USER", "")
    password = getenv("DATAIMPULSE_PASS", "")
    country = getenv("DATAIMPULSE_COUNTRY", "").strip().lower()
    sessttl = _whole_number("DATAIMPULSE_SESSTTL", default=3)

    if not user or not password:
        msg = "missing DATAIMPULSE_USER or DATAIMPULSE_PASS"
        raise RuntimeError(msg)
    if not country:
        msg = "DATAIMPULSE_COUNTRY must not be empty (Peru exits, e.g. pe)"
        raise RuntimeError(msg)
    if sessttl < 1:
        msg = "DATAIMPULSE_SESSTTL must be >= 1 minute"
        raise RuntimeError(msg)

    return _DataImpulseConfig(
        user=user,
        password=password,
        country=country,
        sessttl=sessttl,
    )
