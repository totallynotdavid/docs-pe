from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from browser.proxy import ProxyEndpoint, load_proxy_provider


if TYPE_CHECKING:
    from browser.proxy import ProxyProvider


# A path that does not exist: load_dotenv is a no-op on it, so each provider
# reads exactly the env vars the test set and nothing from a real .env.
_NO_ENV_FILE = "/nonexistent/.env"

_ALL_PROXY_ENV = (
    "PROXY_PROVIDER",
    "GEONODE_USER",
    "GEONODE_PASS",
    "GEONODE_GATEWAY",
    "GEONODE_TYPE",
    "GEONODE_COUNTRY",
    "GEONODE_LIFETIME",
    "DATAIMPULSE_USER",
    "DATAIMPULSE_PASS",
    "DATAIMPULSE_COUNTRY",
    "DATAIMPULSE_SESSTTL",
)


def _geonode(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> ProxyProvider:
    env = {
        "PROXY_PROVIDER": "geonode",
        "GEONODE_USER": "u",
        "GEONODE_PASS": "p",
        "GEONODE_COUNTRY": "PE",
        **overrides,
    }
    _set_env(monkeypatch, env)
    return load_proxy_provider(env_file=_NO_ENV_FILE)


def _dataimpulse(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> ProxyProvider:
    env = {
        "PROXY_PROVIDER": "dataimpulse",
        "DATAIMPULSE_USER": "u",
        "DATAIMPULSE_PASS": "p",
        "DATAIMPULSE_COUNTRY": "pe",
        **overrides,
    }
    _set_env(monkeypatch, env)
    return load_proxy_provider(env_file=_NO_ENV_FILE)


def _set_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
    for name in _ALL_PROXY_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def test_endpoint_carries_upstream_host_and_credentials() -> None:
    endpoint = ProxyEndpoint(
        host="proxy.geonode.io", port="10000", username="u-session-x", password="p"
    )
    assert (endpoint.host, endpoint.port) == ("proxy.geonode.io", "10000")
    assert (endpoint.username, endpoint.password) == ("u-session-x", "p")


def test_geonode_endpoint_encodes_country_and_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _geonode(monkeypatch, GEONODE_LIFETIME="10", GEONODE_TYPE="residential")
    endpoint = provider.new_endpoint()
    assert endpoint.host == "proxy.geonode.io"
    assert endpoint.port == "10000"
    assert "-type-residential" in endpoint.username
    assert "-country-PE" in endpoint.username
    assert "-lifetime-10" in endpoint.username


def test_geonode_rotates_the_session_id_each_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _geonode(monkeypatch)
    assert provider.new_endpoint().username != provider.new_endpoint().username


def test_dataimpulse_endpoint_carries_country_and_sessttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _dataimpulse(monkeypatch, DATAIMPULSE_SESSTTL="3")
    endpoint = provider.new_endpoint()
    assert endpoint.host == "gw.dataimpulse.com"
    assert endpoint.port == "823"
    assert "__cr.pe" in endpoint.username
    assert ";sessttl.3" in endpoint.username


def test_load_takes_the_first_of_several_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A shared .env may list both for fetch's pool; browser drives one session.
    provider = _dataimpulse(monkeypatch, PROXY_PROVIDER="dataimpulse,geonode")
    assert provider.name == "dataimpulse"


def test_load_without_provider_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, {})
    with pytest.raises(RuntimeError, match="PROXY_PROVIDER must be set"):
        load_proxy_provider(env_file=_NO_ENV_FILE)


def test_load_geonode_without_country_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(
        monkeypatch,
        {"PROXY_PROVIDER": "geonode", "GEONODE_USER": "u", "GEONODE_PASS": "p"},
    )
    with pytest.raises(RuntimeError, match="GEONODE_COUNTRY"):
        load_proxy_provider(env_file=_NO_ENV_FILE)
