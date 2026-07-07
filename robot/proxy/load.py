from __future__ import annotations

from os import getenv
from typing import TYPE_CHECKING

from dotenv import load_dotenv

from robot.proxy.dataimpulse import DataImpulseProvider, load_dataimpulse_config
from robot.proxy.geonode import GeoNodeProvider, load_geonode_config


if TYPE_CHECKING:
    from robot.proxy.base import ProxyProvider


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
    if name == "geonode":
        return GeoNodeProvider(load_geonode_config(env_file=env_file))
    return DataImpulseProvider(load_dataimpulse_config(env_file=env_file))
