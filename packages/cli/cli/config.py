from __future__ import annotations

from dataclasses import replace
from os import getenv
from typing import TYPE_CHECKING

from core.domain.errors import ProxyConfigurationError
from core.proxy.base import values_from_environment
from core.proxy.registry import PROVIDERS, spec_for


if TYPE_CHECKING:
    from core.proxy.base import ProxyProvider

_EXAMPLE = "geonode:30,dataimpulse:18"


def load_proxy_providers() -> list[ProxyProvider]:
    raw = getenv("PROXY_PROVIDER", "").strip()

    if not raw:
        allowed = "|".join(sorted(PROVIDERS))
        msg = f"PROXY_PROVIDER must be set (comma-separated {allowed}, e.g. {_EXAMPLE})"
        raise ProxyConfigurationError(msg)

    requests = [_parse(chunk) for chunk in raw.split(",") if chunk.strip()]
    seen: set[str] = set()
    providers: list[ProxyProvider] = []

    for name, workers in requests:
        if name in seen:
            msg = f"PROXY_PROVIDER lists {name!r} more than once"
            raise ProxyConfigurationError(msg)

        seen.add(name)

        spec = spec_for(name)
        provider = spec.build(values_from_environment(spec))

        if workers is not None:
            provider.tuning = replace(provider.tuning, workers=workers)

        providers.append(provider)

    return providers


def _parse(chunk: str) -> tuple[str, int | None]:
    name, separator, raw_workers = chunk.strip().lower().partition(":")
    name = name.strip()

    spec_for(name)

    if not separator:
        return name, None

    raw_workers = raw_workers.strip()

    if not raw_workers.isdigit() or int(raw_workers) < 1:
        msg = (
            f"PROXY_PROVIDER lane count for {name!r} must be a positive integer, "
            f"got {raw_workers!r} (syntax: {_EXAMPLE})"
        )
        raise ProxyConfigurationError(msg)

    return name, int(raw_workers)
