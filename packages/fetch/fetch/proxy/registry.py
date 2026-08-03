from __future__ import annotations

from typing import TYPE_CHECKING

from fetch.domain.errors import ProxyConfigurationError
from fetch.proxy.dataimpulse import DATAIMPULSE
from fetch.proxy.egress import resolve_egress_ip
from fetch.proxy.geonode import GEONODE


if TYPE_CHECKING:
    from collections.abc import Mapping

    from fetch.proxy.base import ProviderSpec, ProxyProvider


PROVIDERS: dict[str, ProviderSpec] = {
    GEONODE.name: GEONODE,
    DATAIMPULSE.name: DATAIMPULSE,
}


def spec_for(name: str) -> ProviderSpec:
    try:
        return PROVIDERS[name]
    except KeyError:
        allowed = "|".join(sorted(PROVIDERS))
        msg = f"unknown proxy provider {name!r}; choose from {allowed}"

        raise ProxyConfigurationError(msg) from None


def provider_from_values(name: str, raw: Mapping[str, str]) -> ProxyProvider:
    spec = spec_for(name)

    return spec.build(spec.normalize(raw))


async def preflight(name: str, raw: Mapping[str, str]) -> str:
    """Open a real provider session and return its exit IP."""
    provider = provider_from_values(name, raw)
    session = provider.new_session(slot_id=1)

    try:
        egress_ip = await resolve_egress_ip(session)
    finally:
        await provider.release(session)

    if not egress_ip:
        msg = "proxy configuration did not reach the internet"

        raise ProxyConfigurationError(msg)

    return egress_ip
