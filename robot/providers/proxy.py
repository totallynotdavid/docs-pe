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
    """Operational defaults a provider recommends for itself.

    These are gateway-tolerance knobs, not OSIPTEL knobs, so each provider owns
    its own baseline rather than sharing one global default. CLI flags override
    them; the merge lives in robot.pipeline.run.
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


def load_proxy_provider(*, env_file: str) -> ProxyProvider:
    load_dotenv(env_file, override=False)
    name = getenv("PROXY_PROVIDER", "geonode").strip().lower()
    # Lazy imports: the provider modules import ProxySession/ProviderTuning from
    # this module, so importing them at module scope here would be circular.
    if name == "geonode":
        from robot.providers.geonode import GeoNodeProvider, load_geonode_config

        return GeoNodeProvider(load_geonode_config(env_file=env_file))
    if name == "dataimpulse":
        from robot.providers.dataimpulse import (
            DataImpulseProvider,
            load_dataimpulse_config,
        )

        return DataImpulseProvider(load_dataimpulse_config(env_file=env_file))
    msg = f"PROXY_PROVIDER must be one of geonode|dataimpulse, got {name!r}"
    raise RuntimeError(msg)
