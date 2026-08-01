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


def test_a_lane_count_overrides_that_providers_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_provider_creds(monkeypatch)
    monkeypatch.setenv("PROXY_PROVIDER", "geonode:30,dataimpulse:18")
    providers = load_proxy_providers(env_file=_ENV_FILE)
    assert [p.tuning.workers for p in providers] == [30, 18]


def test_an_omitted_lane_count_keeps_the_provider_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_valid_provider_creds(monkeypatch)
    monkeypatch.setenv("PROXY_PROVIDER", "geonode,dataimpulse:18")
    geonode, dataimpulse = load_proxy_providers(env_file=_ENV_FILE)
    assert geonode.tuning.workers == GeoNodeProvider.tuning.workers
    assert dataimpulse.tuning.workers == 18


def test_overriding_one_provider_does_not_leak_into_the_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # tuning is a class attribute, so an override must shadow it per instance
    # rather than mutate the shared default for every later run.
    _set_valid_provider_creds(monkeypatch)
    monkeypatch.setenv("PROXY_PROVIDER", "geonode:30")
    default = GeoNodeProvider.tuning.workers
    assert load_proxy_providers(env_file=_ENV_FILE)[0].tuning.workers == 30
    assert GeoNodeProvider.tuning.workers == default


@pytest.mark.parametrize(
    "value", ["geonode:0", "geonode:-1", "geonode:abc", "geonode:"]
)
def test_an_invalid_lane_count_raises(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    _set_valid_provider_creds(monkeypatch)
    monkeypatch.setenv("PROXY_PROVIDER", value)
    with pytest.raises(RuntimeError, match="lane count"):
        load_proxy_providers(env_file=_ENV_FILE)
