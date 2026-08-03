from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from browser.proxy import ProxyEndpoint, load_proxy_provider


if TYPE_CHECKING:
    from browser.proxy import ProxyProvider


_ALL_PROXY_ENV = (
    "PROXY_PROVIDER",
    "GEONODE_USERNAME",
    "GEONODE_PASSWORD",
    "GEONODE_GATEWAY",
    "GEONODE_PROXY_TYPE",
    "GEONODE_COUNTRY",
    "GEONODE_LIFETIME_MINUTES",
    "DATAIMPULSE_USERNAME",
    "DATAIMPULSE_PASSWORD",
    "DATAIMPULSE_COUNTRY",
    "DATAIMPULSE_SESSION_MINUTES",
)


def _geonode(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> ProxyProvider:
    env = {
        "PROXY_PROVIDER": "geonode",
        "GEONODE_USERNAME": "u",
        "GEONODE_PASSWORD": "p",
        "GEONODE_COUNTRY": "PE",
        **overrides,
    }
    _set_env(monkeypatch, env)
    return load_proxy_provider()


def _dataimpulse(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> ProxyProvider:
    env = {
        "PROXY_PROVIDER": "dataimpulse",
        "DATAIMPULSE_USERNAME": "u",
        "DATAIMPULSE_PASSWORD": "p",
        "DATAIMPULSE_COUNTRY": "pe",
        **overrides,
    }
    _set_env(monkeypatch, env)
    return load_proxy_provider()


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
    provider = _geonode(
        monkeypatch,
        GEONODE_LIFETIME_MINUTES="10",
        GEONODE_PROXY_TYPE="residential",
    )
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
    provider = _dataimpulse(monkeypatch, DATAIMPULSE_SESSION_MINUTES="3")
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
        load_proxy_provider()


def test_load_geonode_without_country_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(
        monkeypatch,
        {
            "PROXY_PROVIDER": "geonode",
            "GEONODE_USERNAME": "u",
            "GEONODE_PASSWORD": "p",
        },
    )
    with pytest.raises(RuntimeError, match="GEONODE_COUNTRY"):
        load_proxy_provider()
