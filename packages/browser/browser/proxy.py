from __future__ import annotations

import uuid

from dataclasses import dataclass
from os import getenv
from typing import Protocol


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
    session_minutes: int


class DataImpulseProvider:
    name = "dataimpulse"

    def __init__(self, config: _DataImpulseConfig) -> None:
        self._config = config

    def new_endpoint(self) -> ProxyEndpoint:
        config = self._config
        username = (
            f"{config.user}__cr.{config.country}"
            f";sessid.{_new_session_id()}"
            f";sessttl.{config.session_minutes}"
        )

        return ProxyEndpoint(
            host=_DATAIMPULSE_HOST,
            port=_DATAIMPULSE_PORT,
            username=username,
            password=config.password,
        )


def load_proxy_provider() -> ProxyProvider:
    """Build the first provider listed in PROXY_PROVIDER."""
    raw = getenv("PROXY_PROVIDER", "").strip()

    if not raw:
        msg = "PROXY_PROVIDER must be set (geonode or dataimpulse), or pass --no-proxy"
        raise RuntimeError(msg)

    first_entry = next(
        (entry.strip() for entry in raw.split(",") if entry.strip()),
        "",
    )
    name = first_entry.partition(":")[0].lower()

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
    user = getenv("GEONODE_USERNAME", "")
    password = getenv("GEONODE_PASSWORD", "")
    gateway = getenv("GEONODE_GATEWAY", "fr").strip().lower()
    proxy_type = getenv("GEONODE_PROXY_TYPE", "residential").strip().lower()
    country = getenv("GEONODE_COUNTRY", "").strip().upper()
    lifetime = _whole_number("GEONODE_LIFETIME_MINUTES", default=10)

    if not user or not password:
        msg = "missing GEONODE_USERNAME or GEONODE_PASSWORD"
        raise RuntimeError(msg)

    if not country:
        msg = "GEONODE_COUNTRY must be set (Peru exits, e.g. PE)"
        raise RuntimeError(msg)

    if gateway not in _GEONODE_GATEWAY_HOST:
        allowed = "|".join(sorted(_GEONODE_GATEWAY_HOST))
        msg = f"GEONODE_GATEWAY must be one of {allowed}"
        raise RuntimeError(msg)

    if proxy_type not in {"residential", "datacenter", "mix"}:
        msg = "GEONODE_PROXY_TYPE must be one of residential|datacenter|mix"
        raise RuntimeError(msg)

    if not 3 <= lifetime <= 1440:
        msg = "GEONODE_LIFETIME_MINUTES must be between 3 and 1440 minutes"
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
    user = getenv("DATAIMPULSE_USERNAME", "")
    password = getenv("DATAIMPULSE_PASSWORD", "")
    country = getenv("DATAIMPULSE_COUNTRY", "").strip().lower()
    session_minutes = _whole_number(
        "DATAIMPULSE_SESSION_MINUTES",
        default=3,
    )

    if not user or not password:
        msg = "missing DATAIMPULSE_USERNAME or DATAIMPULSE_PASSWORD"
        raise RuntimeError(msg)

    if not country:
        msg = "DATAIMPULSE_COUNTRY must be set (Peru exits, e.g. pe)"
        raise RuntimeError(msg)

    if session_minutes < 1:
        msg = "DATAIMPULSE_SESSION_MINUTES must be >= 1 minute"
        raise RuntimeError(msg)

    return _DataImpulseConfig(
        user=user,
        password=password,
        country=country,
        session_minutes=session_minutes,
    )
