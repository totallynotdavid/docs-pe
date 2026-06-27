from __future__ import annotations

from dataclasses import dataclass
from os import getenv
from typing import Protocol

from dotenv import load_dotenv


@dataclass(frozen=True)
class ProxySession:
    """One sticky proxy session: a fully-formed upstream the lane can dial.

    Provider-neutral on purpose. The lane and the OSIPTEL client only need the
    proxy URL plus the two ids for logging, so nothing here records which
    provider minted the session.
    """

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
    """Operational defaults a provider owns for itself.

    These are gateway-tolerance knobs, not OSIPTEL knobs, so each provider owns
    its own baseline rather than sharing one global default. There is no CLI
    override: the provider's tuning is the single source for its lane count and
    ban cooldown, which is what lets several providers run side by side, each at
    its own measured width, against one shared queue.
    """

    workers: int
    ban_cooldown_s: float


class ProxyProvider(Protocol):
    """The one seam between the pipeline and a proxy gateway.

    Everything provider-specific (credentials, session shape, release semantics,
    recommended tuning) lives behind this. The lane stays provider-blind.
    """

    name: str
    tuning: ProviderTuning

    def new_session(self, *, slot_id: int) -> ProxySession: ...

    async def release(self, session: ProxySession) -> None: ...


_KNOWN_PROVIDERS = ("geonode", "dataimpulse")


def load_proxy_providers(*, env_file: str) -> list[ProxyProvider]:
    """Construct every provider named in PROXY_PROVIDER (comma-separated).

    One name runs one provider; several run side by side, each contributing its
    own lanes to the shared queue (no second env file, no manual sharding).
    Fails hard rather than guessing: PROXY_PROVIDER must be set, every name must
    be known and distinct, and each named provider's env block must validate.
    """
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
