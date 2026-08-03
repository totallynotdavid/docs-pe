from __future__ import annotations

from dataclasses import dataclass, replace
from os import getenv
from typing import TYPE_CHECKING

from dotenv import load_dotenv

from fetch.domain.errors import ProxyConfigurationError
from fetch.proxy.registry import PROVIDERS, spec_for


if TYPE_CHECKING:
    from fetch.proxy.base import ProviderSpec, ProxyProvider


_SYNTAX = "geonode:30,dataimpulse:18"


@dataclass(frozen=True)
class _ProviderSpecRequest:
    name: str
    # None means "use the provider's own tuned default".
    workers: int | None


def load_proxy_providers(*, env_file: str) -> list[ProxyProvider]:
    load_dotenv(env_file, override=False)
    raw = getenv("PROXY_PROVIDER", "").strip()
    if not raw:
        allowed = "|".join(sorted(PROVIDERS))
        msg = f"PROXY_PROVIDER must be set (comma-separated {allowed}, e.g. {_SYNTAX})"
        raise ProxyConfigurationError(msg)

    requests = [_parse(chunk) for chunk in raw.split(",") if chunk.strip()]
    seen: set[str] = set()
    for request in requests:
        if request.name in seen:
            msg = f"PROXY_PROVIDER lists {request.name!r} more than once"
            raise ProxyConfigurationError(msg)
        seen.add(request.name)

    return [_build(request) for request in requests]


def values_from_environment(spec: ProviderSpec) -> dict[str, str]:
    """Read a provider's configuration from `<PROVIDER>_<FIELD>` variables."""
    raw = {
        field.name: getenv(f"{spec.name}_{field.name}".upper(), field.default)
        for field in spec.fields
    }
    return spec.normalize(raw)


def _parse(chunk: str) -> _ProviderSpecRequest:
    # "geonode" takes the provider's default lane count, "geonode:30" overrides it.
    name, sep, workers = chunk.strip().lower().partition(":")
    name = name.strip()
    spec_for(name)
    if not sep:
        return _ProviderSpecRequest(name=name, workers=None)

    workers = workers.strip()
    if not workers.isdigit() or int(workers) < 1:
        msg = (
            f"PROXY_PROVIDER lane count for {name!r} must be a positive integer, "
            f"got {workers!r} (syntax: {_SYNTAX})"
        )
        raise ProxyConfigurationError(msg)
    return _ProviderSpecRequest(name=name, workers=int(workers))


def _build(request: _ProviderSpecRequest) -> ProxyProvider:
    spec = spec_for(request.name)
    provider = spec.build(values_from_environment(spec))
    if request.workers is not None:
        # Lane count is deployment capacity, so it is applied here and a new provider
        # needs no code to become tunable.
        provider.tuning = replace(provider.tuning, workers=request.workers)
    return provider
