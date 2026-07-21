from __future__ import annotations

import pytest

from fetch.proxy.geonode import (
    _HTTP_STICKY_PORT_MAX,
    _HTTP_STICKY_PORT_MIN,
    GeoNodeConfig,
    GeoNodeProvider,
    build_username,
    load_geonode_config,
    slot_port,
)


# A path that never exists, so load_dotenv's own file lookup is a silent no-op.
_ENV_FILE = "/nonexistent/does-not-exist.env"

_ENV_VARS = (
    "GEONODE_USER",
    "GEONODE_PASS",
    "GEONODE_GATEWAY",
    "GEONODE_TYPE",
    "GEONODE_COUNTRY",
    "GEONODE_STATE",
    "GEONODE_CITY",
    "GEONODE_ASN",
    "GEONODE_STRICT_OFF",
    "GEONODE_LIFETIME",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _set_minimal_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEONODE_USER", "user")
    monkeypatch.setenv("GEONODE_PASS", "pass")
    monkeypatch.setenv("GEONODE_COUNTRY", "PE")


def test_missing_user_or_pass_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEONODE_COUNTRY", "PE")
    with pytest.raises(RuntimeError, match="GEONODE_USER or GEONODE_PASS"):
        load_geonode_config(env_file=_ENV_FILE)


def test_missing_country_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # OSIPTEL's WAF blocks foreign exits; this must fail loudly.
    monkeypatch.setenv("GEONODE_USER", "user")
    monkeypatch.setenv("GEONODE_PASS", "pass")
    with pytest.raises(RuntimeError, match="GEONODE_COUNTRY must be set"):
        load_geonode_config(env_file=_ENV_FILE)


def test_unknown_gateway_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_minimal_valid_env(monkeypatch)
    monkeypatch.setenv("GEONODE_GATEWAY", "de")
    with pytest.raises(RuntimeError, match="GEONODE_GATEWAY"):
        load_geonode_config(env_file=_ENV_FILE)


def test_invalid_proxy_type_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_minimal_valid_env(monkeypatch)
    monkeypatch.setenv("GEONODE_TYPE", "bogus")
    with pytest.raises(RuntimeError, match="GEONODE_TYPE"):
        load_geonode_config(env_file=_ENV_FILE)


@pytest.mark.parametrize("lifetime", ["2", "1441"])
def test_lifetime_out_of_range_raises(
    monkeypatch: pytest.MonkeyPatch, lifetime: str
) -> None:
    _set_minimal_valid_env(monkeypatch)
    monkeypatch.setenv("GEONODE_LIFETIME", lifetime)
    with pytest.raises(RuntimeError, match="GEONODE_LIFETIME"):
        load_geonode_config(env_file=_ENV_FILE)


def test_happy_path_applies_defaults_for_everything_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_minimal_valid_env(monkeypatch)
    config = load_geonode_config(env_file=_ENV_FILE)
    assert config == GeoNodeConfig(
        user="user",
        password="pass",
        host="proxy.geonode.io",
        proxy_type="residential",
        country="PE",
        state="",
        city="",
        asn="",
        strict_off=False,
        lifetime=10,
    )


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
    with pytest.raises(ValueError, match="slot_id must be >= 1"):
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
    with pytest.raises(ValueError, match="slot_id must be <="):
        GeoNodeProvider(_config()).new_session(slot_id=max_slots + 1)
