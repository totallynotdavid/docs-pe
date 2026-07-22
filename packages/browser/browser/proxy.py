from __future__ import annotations

import uuid

from dataclasses import dataclass
from os import getenv
from typing import Protocol

from dotenv import load_dotenv


# This mirrors fetch/proxy's env contract on purpose: the same .env, the same
# PROXY_PROVIDER / GEONODE_* / DATAIMPULSE_* variables, the same vendor username
# formats. It is a deliberate copy, not a shared import, exactly like
# browser/subject.py mirrors fetch's Doc: the two packages stay independent.
# Browser drives ONE Chrome session at a time, so this copy is far smaller than
# fetch's: no per-worker sticky ports, no async release, no egress probing.
# It only has to hand Chrome a connection string; rotation is the run loop
# minting a fresh exit each time it (re)opens a session.


@dataclass(frozen=True)
class ProxyEndpoint:
    """One proxy exit as a Chrome-ready connection string.

    SeleniumBase CDP takes proxy="user:pass@host:port" (no scheme) and builds an
    auth extension from it, so that is the shape we return. Never log the string:
    it carries the account password.
    """

    host: str
    port: str
    username: str
    password: str

    def as_chrome_proxy(self) -> str:
        return f"{self.username}:{self.password}@{self.host}:{self.port}"


class ProxyProvider(Protocol):
    name: str

    def new_endpoint(self) -> ProxyEndpoint: ...


_GEONODE_GATEWAY_HOST = {
    "fr": "proxy.geonode.io",
    "fr_whitelist": "prod-proxy.geonode.io",
    "us": "us.proxy.geonode.io",
    "sg": "sg.proxy.geonode.io",
}
# One session at a time, so one dedicated sticky port is enough. Fetch spreads
# lanes across 10000..10900; browser only ever needs the first.
_GEONODE_STICKY_PORT = "10000"

_DATAIMPULSE_HOST = "gw.dataimpulse.com"
_DATAIMPULSE_PORT = "823"
_DATAIMPULSE_DEFAULT_SESSTTL = 3

_KNOWN_PROVIDERS = ("geonode", "dataimpulse")


@dataclass(frozen=True)
class _GeoNodeConfig:
    user: str
    password: str
    host: str
    proxy_type: str
    country: str
    lifetime: int


class GeoNodeProvider:
    """GeoNode sticky sessions: a fresh session id per endpoint pins a new exit IP.

    The run loop calls new_endpoint() once per browser session, so each session
    restart (a ban) rotates to a fresh Peru exit.
    """

    name = "geonode"

    def __init__(self, config: _GeoNodeConfig) -> None:
        self._config = config

    def new_endpoint(self) -> ProxyEndpoint:
        config = self._config
        session_id = f"b_{uuid.uuid4().hex[:8]}"
        username = (
            f"{config.user}-session-{session_id}"
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
    """DataImpulse sticky sessions keyed by a per-session sessid in the username.

    Sessions expire by sessttl, so there is nothing to release; a fresh sessid
    per endpoint pins a new exit IP.
    """

    name = "dataimpulse"

    def __init__(self, config: _DataImpulseConfig) -> None:
        self._config = config

    def new_endpoint(self) -> ProxyEndpoint:
        config = self._config
        session_id = f"b_{uuid.uuid4().hex[:8]}"
        username = (
            f"{config.user}__cr.{config.country}"
            f";sessid.{session_id};sessttl.{config.sessttl}"
        )
        return ProxyEndpoint(
            host=_DATAIMPULSE_HOST,
            port=_DATAIMPULSE_PORT,
            username=username,
            password=config.password,
        )


def load_proxy_provider(*, env_file: str) -> ProxyProvider:
    """Construct the proxy provider selected by PROXY_PROVIDER, or fail fast.

    Browser runs one session at a time, so it uses a single provider. A shared
    .env may list several for fetch's worker pool (e.g. "geonode,dataimpulse");
    browser takes the first named.
    """
    load_dotenv(env_file, override=False)
    raw = getenv("PROXY_PROVIDER", "").strip()
    if not raw:
        msg = "PROXY_PROVIDER must be set (geonode or dataimpulse), or pass --no-proxy"
        raise RuntimeError(msg)

    names = [chunk.strip().lower() for chunk in raw.split(",") if chunk.strip()]
    name = names[0]
    if name not in _KNOWN_PROVIDERS:
        allowed = "|".join(_KNOWN_PROVIDERS)
        msg = f"PROXY_PROVIDER entry {name!r} is not one of {allowed}"
        raise RuntimeError(msg)

    if name == "geonode":
        return GeoNodeProvider(_load_geonode_config())
    return DataImpulseProvider(_load_dataimpulse_config())


def _load_geonode_config() -> _GeoNodeConfig:
    user = getenv("GEONODE_USER", "")
    password = getenv("GEONODE_PASS", "")
    gateway = getenv("GEONODE_GATEWAY", "fr")
    proxy_type = getenv("GEONODE_TYPE", "residential")
    country = getenv("GEONODE_COUNTRY", "")
    lifetime_raw = getenv("GEONODE_LIFETIME", "").strip()
    lifetime = int(lifetime_raw) if lifetime_raw else 10

    if not user or not password:
        msg = "missing GEONODE_USER or GEONODE_PASS"
        raise RuntimeError(msg)
    if not country:
        # Peru sites geo-gate or geo-score foreign exits; an unset country
        # silently routes through the wrong region, so fail loudly instead.
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
    sessttl_raw = getenv("DATAIMPULSE_SESSTTL", "").strip()
    sessttl = int(sessttl_raw) if sessttl_raw else _DATAIMPULSE_DEFAULT_SESSTTL

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
