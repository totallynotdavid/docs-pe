from __future__ import annotations

import pytest

from fetch.domain.errors import ProxyConfigurationError
from fetch.proxy.geonode import (
    _HTTP_STICKY_PORT_MAX,
    _HTTP_STICKY_PORT_MIN,
    GEONODE,
    GeoNodeConfig,
    GeoNodeProvider,
    build_username,
    slot_port,
)
from fetch.proxy.load import values_from_environment


# Variable names come from the field schema, so this list cannot drift from it.
_ENV_VARS = tuple(f"GEONODE_{field.name}".upper() for field in GEONODE.fields)


def _load() -> dict[str, str]:
    return values_from_environment(GEONODE)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _set_minimal_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEONODE_USERNAME", "user")
    monkeypatch.setenv("GEONODE_PASSWORD", "pass")
    monkeypatch.setenv("GEONODE_COUNTRY", "PE")


def test_missing_username_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEONODE_COUNTRY", "PE")
    with pytest.raises(ProxyConfigurationError, match="username is required"):
        _load()


def test_missing_country_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # OSIPTEL's WAF blocks foreign exits; this must fail loudly.
    monkeypatch.setenv("GEONODE_USERNAME", "user")
    monkeypatch.setenv("GEONODE_PASSWORD", "pass")
    monkeypatch.setenv("GEONODE_COUNTRY", "")
    with pytest.raises(ProxyConfigurationError, match="country is required"):
        _load()


def test_a_malformed_country_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_minimal_valid_env(monkeypatch)
    monkeypatch.setenv("GEONODE_COUNTRY", "peru")
    with pytest.raises(ProxyConfigurationError, match="two-letter country code"):
        _load()


def test_unknown_gateway_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_minimal_valid_env(monkeypatch)
    monkeypatch.setenv("GEONODE_GATEWAY", "de")
    with pytest.raises(ProxyConfigurationError, match="gateway must be one of"):
        _load()


def test_invalid_proxy_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_minimal_valid_env(monkeypatch)
    monkeypatch.setenv("GEONODE_PROXY_TYPE", "bogus")
    with pytest.raises(ProxyConfigurationError, match="proxy_type must be one of"):
        _load()


@pytest.mark.parametrize("lifetime", ["2", "1441"])
def test_lifetime_out_of_range_raises(
    monkeypatch: pytest.MonkeyPatch, lifetime: str
) -> None:
    _set_minimal_valid_env(monkeypatch)
    monkeypatch.setenv("GEONODE_LIFETIME_MINUTES", lifetime)
    with pytest.raises(ProxyConfigurationError, match="lifetime_minutes must be"):
        _load()


def test_happy_path_applies_defaults_for_everything_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_minimal_valid_env(monkeypatch)
    assert _load() == {
        "username": "user",
        "password": "pass",
        "gateway": "fr",
        "proxy_type": "residential",
        "country": "PE",
        "state": "",
        "city": "",
        "asn": "",
        "strict_off": "",
        "lifetime_minutes": "10",
    }


def test_build_turns_normalized_values_into_a_gateway_bound_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The gateway name is a field; the host it resolves to is not, so `build` is
    # the only place that mapping lives.
    _set_minimal_valid_env(monkeypatch)
    monkeypatch.setenv("GEONODE_GATEWAY", "sg")
    session = GEONODE.build(_load()).new_session(slot_id=1)
    assert session.host == "sg.proxy.geonode.io"


def test_build_username_includes_every_set_optional_field() -> None:
    config = GeoNodeConfig(
        user="u",
        password="p",
        host="h",
        proxy_type="residential",
        country="PE",
        state="Lima",
        city="Lima",
        asn="1234",
        strict_off=True,
        lifetime=10,
    )
    username = build_username(config, session_id="sid")
    assert username == (
        "u-session-sid-type-residential-country-PE-state-Lima-city-Lima"
        "-asn-1234-strict-off-lifetime-10"
    )


def test_build_username_omits_unset_optional_fields() -> None:
    config = GeoNodeConfig(
        user="u",
        password="p",
        host="h",
        proxy_type="residential",
        country="PE",
        state="",
        city="",
        asn="",
        strict_off=False,
        lifetime=0,
    )
    username = build_username(config, session_id="sid")
    assert username == "u-session-sid-type-residential-country-PE"


@pytest.mark.parametrize(
    ("slot_id", "expected"),
    [
        (1, _HTTP_STICKY_PORT_MIN),
        (2, _HTTP_STICKY_PORT_MIN + 1),
        (901, _HTTP_STICKY_PORT_MAX),
    ],
)
def test_slot_port_starts_at_the_minimum_and_climbs_one_per_slot(
    slot_id: int, expected: int
) -> None:
    assert slot_port(slot_id=slot_id) == expected


@pytest.mark.parametrize("slot_id", [0, -1])
def test_slot_id_below_one_raises(slot_id: int) -> None:
    with pytest.raises(ValueError, match="slot_id must be between 1 and"):
        slot_port(slot_id=slot_id)


def _config() -> GeoNodeConfig:
    return GeoNodeConfig(
        user="u",
        password="p",
        host="h",
        proxy_type="residential",
        country="PE",
        state="",
        city="",
        asn="",
        strict_off=False,
        lifetime=10,
    )


def test_new_session_derives_the_proxy_id_and_port_from_the_slot() -> None:
    session = GeoNodeProvider(_config()).new_session(slot_id=1)
    assert session.proxy_id == f"proxy-1-port-{_HTTP_STICKY_PORT_MIN}"
    assert session.port == str(_HTTP_STICKY_PORT_MIN)
    assert session.host == "h"
    assert session.password == "p"


def test_new_session_raises_past_the_max_slot() -> None:
    max_slots = _HTTP_STICKY_PORT_MAX - _HTTP_STICKY_PORT_MIN + 1
    with pytest.raises(ValueError, match="slot_id must be between 1 and"):
        GeoNodeProvider(_config()).new_session(slot_id=max_slots + 1)
