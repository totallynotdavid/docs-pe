from __future__ import annotations

from dataclasses import dataclass, replace
from os import getenv
from typing import TYPE_CHECKING

from fetch.domain.errors import ProxyConfigurationError
from fetch.proxy.registry import PROVIDERS, spec_for


if TYPE_CHECKING:
    from fetch.proxy.base import ProviderSpec, ProxyProvider

_EXAMPLE = "geonode:30,dataimpulse:18"


@dataclass(frozen=True)
class _ProviderSpecRequest:
    name: str
    workers: int | None


def load_proxy_providers() -> list[ProxyProvider]:
    raw = getenv("PROXY_PROVIDER", "").strip()

    if not raw:
        allowed = "|".join(sorted(PROVIDERS))
        msg = f"PROXY_PROVIDER must be set (comma-separated {allowed}, e.g. {_EXAMPLE})"
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
    raw = {
        field.name: getenv(f"{spec.name}_{field.name}".upper(), field.default)
        for field in spec.fields
    }

    return spec.normalize(raw)


def _parse(chunk: str) -> _ProviderSpecRequest:
    name, separator, raw_workers = chunk.strip().lower().partition(":")
    name = name.strip()

    spec_for(name)

    if not separator:
        return _ProviderSpecRequest(name=name, workers=None)

    raw_workers = raw_workers.strip()

    if not raw_workers.isdigit():
        msg = (
            f"PROXY_PROVIDER lane count for {name!r} must be a positive integer, "
            f"got {raw_workers!r} (syntax: {_EXAMPLE})"
        )
        raise ProxyConfigurationError(msg)

    workers = int(raw_workers)

    if workers < 1:
        msg = (
            f"PROXY_PROVIDER lane count for {name!r} must be a positive integer, "
            f"got {raw_workers!r} (syntax: {_EXAMPLE})"
        )
        raise ProxyConfigurationError(msg)

    return _ProviderSpecRequest(name=name, workers=workers)


def _build(request: _ProviderSpecRequest) -> ProxyProvider:
    spec = spec_for(request.name)
    provider = spec.build(values_from_environment(spec))

    if request.workers is not None:
        provider.tuning = replace(provider.tuning, workers=request.workers)

    return provider
