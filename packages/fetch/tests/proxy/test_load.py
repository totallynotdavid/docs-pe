from __future__ import annotations

import pytest

from fetch.proxy.dataimpulse import DataImpulseProvider
from fetch.proxy.geonode import GeoNodeProvider
from fetch.proxy.load import load_proxy_providers


_ENV_FILE = "/nonexistent/does-not-exist.env"

_ENV_VARS = (
    "PROXY_PROVIDER",
    "GEONODE_USER",
    "GEONODE_PASS",
    "GEONODE_COUNTRY",
    "DATAIMPULSE_USER",
    "DATAIMPULSE_PASS",
    "DATAIMPULSE_COUNTRY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _set_valid_provider_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEONODE_USER", "user")
    monkeypatch.setenv("GEONODE_PASS", "pass")
    monkeypatch.setenv("GEONODE_COUNTRY", "PE")
    monkeypatch.setenv("DATAIMPULSE_USER", "user")
    monkeypatch.setenv("DATAIMPULSE_PASS", "pass")
    monkeypatch.setenv("DATAIMPULSE_COUNTRY", "pe")


@pytest.mark.parametrize("value", ["", "   "])
def test_a_missing_or_blank_proxy_provider_raises(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    if value:
        monkeypatch.setenv("PROXY_PROVIDER", value)
    with pytest.raises(RuntimeError, match="PROXY_PROVIDER must be set"):
        load_proxy_providers(env_file=_ENV_FILE)


def test_an_unknown_provider_name_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROXY_PROVIDER", "bogus")
    with pytest.raises(RuntimeError, match="is not one of"):
        load_proxy_providers(env_file=_ENV_FILE)


def test_a_duplicate_provider_name_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_provider_creds(monkeypatch)
    monkeypatch.setenv("PROXY_PROVIDER", "geonode,geonode")
    with pytest.raises(RuntimeError, match="more than once"):
        load_proxy_providers(env_file=_ENV_FILE)


def test_constructs_a_single_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_valid_provider_creds(monkeypatch)
    monkeypatch.setenv("PROXY_PROVIDER", "geonode")
    providers = load_proxy_providers(env_file=_ENV_FILE)
    assert [type(p) for p in providers] == [GeoNodeProvider]


def test_constructs_multiple_providers_in_the_requested_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_provider_creds(monkeypatch)
    monkeypatch.setenv("PROXY_PROVIDER", "dataimpulse,geonode")
    providers = load_proxy_providers(env_file=_ENV_FILE)
    assert [type(p) for p in providers] == [DataImpulseProvider, GeoNodeProvider]


def test_provider_names_are_trimmed_and_lowercased(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_provider_creds(monkeypatch)
    monkeypatch.setenv("PROXY_PROVIDER", " GEONODE , DataImpulse ")
    providers = load_proxy_providers(env_file=_ENV_FILE)
    assert [type(p) for p in providers] == [GeoNodeProvider, DataImpulseProvider]
