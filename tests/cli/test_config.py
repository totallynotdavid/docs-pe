from __future__ import annotations

import pytest

from cli.config import load_proxy_providers
from core.domain.errors import ProxyConfigurationError
from core.proxy.dataimpulse import DataImpulseProvider
from core.proxy.geonode import GeoNodeProvider
from core.proxy.registry import PROVIDERS


_ENV_VARS = (
    "PROXY_PROVIDER",
    *(
        f"{spec.name}_{field.name}".upper()
        for spec in PROVIDERS.values()
        for field in spec.fields
    ),
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _set_valid_provider_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEONODE_USERNAME", "user")
    monkeypatch.setenv("GEONODE_PASSWORD", "pass")
    monkeypatch.setenv("GEONODE_COUNTRY", "PE")
    monkeypatch.setenv("DATAIMPULSE_USERNAME", "user")
    monkeypatch.setenv("DATAIMPULSE_PASSWORD", "pass")
    monkeypatch.setenv("DATAIMPULSE_COUNTRY", "pe")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (None, "PROXY_PROVIDER must be set"),
        ("   ", "PROXY_PROVIDER must be set"),
        ("bogus", "unknown proxy provider"),
        ("geonode,geonode", "more than once"),
    ],
)
def test_an_invalid_provider_list_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
    message: str,
) -> None:
    _set_valid_provider_creds(monkeypatch)
    if value is not None:
        monkeypatch.setenv("PROXY_PROVIDER", value)

    with pytest.raises(ProxyConfigurationError, match=message):
        load_proxy_providers()


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("geonode", [GeoNodeProvider]),
        ("dataimpulse,geonode", [DataImpulseProvider, GeoNodeProvider]),
        (" GEONODE , DataImpulse ", [GeoNodeProvider, DataImpulseProvider]),
    ],
)
def test_providers_are_built_in_the_requested_order(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    expected: list[type],
) -> None:
    _set_valid_provider_creds(monkeypatch)
    monkeypatch.setenv("PROXY_PROVIDER", value)

    assert [type(p) for p in load_proxy_providers()] == expected


@pytest.mark.parametrize(
    ("value", "workers"),
    [
        ("geonode:30,dataimpulse:18", [30, 18]),
        (
            "geonode,dataimpulse:18",
            [GeoNodeProvider.tuning.workers, 18],
        ),
    ],
)
def test_a_lane_count_overrides_only_the_provider_it_names(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
    workers: list[int],
) -> None:
    _set_valid_provider_creds(monkeypatch)
    monkeypatch.setenv("PROXY_PROVIDER", value)

    assert [p.tuning.workers for p in load_proxy_providers()] == workers


def test_overriding_one_provider_does_not_leak_into_the_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Overrides must not mutate the shared class default.
    _set_valid_provider_creds(monkeypatch)
    monkeypatch.setenv("PROXY_PROVIDER", "geonode:30")

    default = GeoNodeProvider.tuning.workers

    assert load_proxy_providers()[0].tuning.workers == 30
    assert GeoNodeProvider.tuning.workers == default


@pytest.mark.parametrize(
    "value",
    [
        "geonode:0",
        "geonode:-1",
        "geonode:abc",
        "geonode:",
    ],
)
def test_an_invalid_lane_count_raises(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    _set_valid_provider_creds(monkeypatch)
    monkeypatch.setenv("PROXY_PROVIDER", value)

    with pytest.raises(ProxyConfigurationError, match="lane count"):
        load_proxy_providers()
