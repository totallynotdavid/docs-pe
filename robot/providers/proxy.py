from __future__ import annotations

from dataclasses import dataclass
from os import getenv
from typing import Protocol

from dotenv import load_dotenv


@dataclass(frozen=True)
class ProxySession:
    proxy_id: str
    host: str
    port: str
    username: str
    password: str
    session_id: str

    def as_http_proxy_url(self) -> str:
        return f"http://{self.username}:{self.password}@{self.host}:{self.port}"


@dataclass(frozen=True)
class ProviderTuning:
    workers: int
    ban_cooldown_s: float


class ProxyProvider(Protocol):
    name: str
    tuning: ProviderTuning

    def new_session(self, *, slot_id: int) -> ProxySession: ...

    async def release(self, session: ProxySession) -> None: ...


_KNOWN_PROVIDERS = ("geonode", "dataimpulse")


def load_proxy_providers(*, env_file: str) -> list[ProxyProvider]:
    load_dotenv(env_file, override=False)
    raw = getenv("PROXY_PROVIDER", "").strip()
    if not raw:
        msg = "PROXY_PROVIDER must be set (comma-separated: geonode,dataimpulse)"
        raise RuntimeError(msg)

    names = [chunk.strip().lower() for chunk in raw.split(",") if chunk.strip()]
    seen: set[str] = set()
    for name in names:
        if name not in _KNOWN_PROVIDERS:
            allowed = "|".join(_KNOWN_PROVIDERS)
            msg = f"PROXY_PROVIDER entry {name!r} is not one of {allowed}"
            raise RuntimeError(msg)
        if name in seen:
            msg = f"PROXY_PROVIDER lists {name!r} more than once"
            raise RuntimeError(msg)
        seen.add(name)

    return [_construct_provider(name, env_file=env_file) for name in names]


def _construct_provider(name: str, *, env_file: str) -> ProxyProvider:
    # Lazy imports: the provider modules import ProxySession/ProviderTuning from
    # this module, so importing them at module scope here would be circular.
    if name == "geonode":
        from robot.providers.geonode import GeoNodeProvider, load_geonode_config

        return GeoNodeProvider(load_geonode_config(env_file=env_file))
    from robot.providers.dataimpulse import (
        DataImpulseProvider,
        load_dataimpulse_config,
    )

    return DataImpulseProvider(load_dataimpulse_config(env_file=env_file))
