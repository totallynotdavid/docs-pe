from __future__ import annotations

from dataclasses import dataclass, replace
from os import getenv
from typing import TYPE_CHECKING

from dotenv import load_dotenv

from fetch.proxy.dataimpulse import DataImpulseProvider, load_dataimpulse_config
from fetch.proxy.geonode import GeoNodeProvider, load_geonode_config


if TYPE_CHECKING:
    from fetch.proxy.base import ProxyProvider


_KNOWN_PROVIDERS = ("geonode", "dataimpulse")

_SYNTAX = "geonode:30,dataimpulse:18"


@dataclass(frozen=True)
class _ProviderSpec:
    name: str
    # None means "use the provider's own tuned default".
    workers: int | None


def load_proxy_providers(*, env_file: str) -> list[ProxyProvider]:
    load_dotenv(env_file, override=False)
    raw = getenv("PROXY_PROVIDER", "").strip()
    if not raw:
        msg = f"PROXY_PROVIDER must be set (comma-separated, e.g. {_SYNTAX})"
        raise RuntimeError(msg)

    specs = [_parse_spec(chunk) for chunk in raw.split(",") if chunk.strip()]
    seen: set[str] = set()
    for spec in specs:
        if spec.name not in _KNOWN_PROVIDERS:
            allowed = "|".join(_KNOWN_PROVIDERS)
            msg = f"PROXY_PROVIDER entry {spec.name!r} is not one of {allowed}"
            raise RuntimeError(msg)
        if spec.name in seen:
            msg = f"PROXY_PROVIDER lists {spec.name!r} more than once"
            raise RuntimeError(msg)
        seen.add(spec.name)

    return [_build(spec, env_file=env_file) for spec in specs]


def _parse_spec(chunk: str) -> _ProviderSpec:
    # "geonode" takes the provider's default lane count, "geonode:30" overrides it.
    # Lane counts live here, beside the provider list, so adding a provider stays a
    # single entry rather than a new variable to keep in sync.
    name, sep, workers = chunk.strip().lower().partition(":")
    name = name.strip()
    if not sep:
        return _ProviderSpec(name=name, workers=None)

    workers = workers.strip()
    if not workers.isdigit():
        msg = (
            f"PROXY_PROVIDER lane count for {name!r} must be a positive integer, "
            f"got {workers!r} (syntax: {_SYNTAX})"
        )
        raise RuntimeError(msg)
    if int(workers) < 1:
        msg = f"PROXY_PROVIDER lane count for {name!r} must be >= 1"
        raise RuntimeError(msg)
    return _ProviderSpec(name=name, workers=int(workers))


def _build(spec: _ProviderSpec, *, env_file: str) -> ProxyProvider:
    provider = _construct_provider(spec.name, env_file=env_file)
    if spec.workers is not None:
        # Lane count is deployment capacity, not a vendor default, so it is applied
        # here rather than inside each provider class -- a new provider then needs
        # no code to become tunable.
        provider.tuning = replace(provider.tuning, workers=spec.workers)
    return provider


def _construct_provider(name: str, *, env_file: str) -> ProxyProvider:
    if name == "geonode":
        return GeoNodeProvider(load_geonode_config(env_file=env_file))
    return DataImpulseProvider(load_dataimpulse_config(env_file=env_file))
