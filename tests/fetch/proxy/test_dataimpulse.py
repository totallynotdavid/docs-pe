from __future__ import annotations

import pytest

from fetch.domain.errors import ProxyConfigurationError
from fetch.proxy.dataimpulse import (
    DATAIMPULSE,
    DataImpulseConfig,
    DataImpulseProvider,
)
from fetch.proxy.load import values_from_environment


_ENV_VARS = tuple(f"DATAIMPULSE_{field.name}".upper() for field in DATAIMPULSE.fields)


def _load() -> dict[str, str]:
    return values_from_environment(DATAIMPULSE)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _set_minimal_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAIMPULSE_USERNAME", "user")
    monkeypatch.setenv("DATAIMPULSE_PASSWORD", "pass")
    monkeypatch.setenv("DATAIMPULSE_COUNTRY", "pe")


def _config() -> DataImpulseConfig:
    return DataImpulseConfig(
        user="user",
        password="pass",
        country="pe",
        sessttl=5,
        host="gw.dataimpulse.com",
    )


def test_missing_username_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAIMPULSE_COUNTRY", "pe")

    with pytest.raises(ProxyConfigurationError, match="username is required"):
        _load()


def test_empty_country_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAIMPULSE_USERNAME", "user")
    monkeypatch.setenv("DATAIMPULSE_PASSWORD", "pass")
    monkeypatch.setenv("DATAIMPULSE_COUNTRY", "")

    with pytest.raises(ProxyConfigurationError, match="country is required"):
        _load()


def test_session_minutes_below_one_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_minimal_valid_env(monkeypatch)
    monkeypatch.setenv("DATAIMPULSE_SESSION_MINUTES", "0")

    with pytest.raises(ProxyConfigurationError, match="session_minutes must be"):
        _load()


def test_country_is_lowercased_and_stripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_minimal_valid_env(monkeypatch)
    monkeypatch.setenv("DATAIMPULSE_COUNTRY", "  PE  ")

    assert _load()["country"] == "pe"


def test_happy_path_defaults_session_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_minimal_valid_env(monkeypatch)

    assert _load() == {
        "username": "user",
        "password": "pass",
        "country": "pe",
        "session_minutes": "3",
    }


def test_build_turns_normalized_values_into_a_live_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_minimal_valid_env(monkeypatch)

    session = DATAIMPULSE.build(_load()).new_session(slot_id=1)

    assert session.host == "gw.dataimpulse.com"
    assert session.username.startswith("user__cr.pe;sessid.")


def test_new_session_embeds_country_sessid_and_sessttl_in_the_username() -> None:
    session = DataImpulseProvider(_config()).new_session(slot_id=2)

    assert session.username.startswith("user__cr.pe;sessid.")
    assert session.username.endswith(";sessttl.5")
    assert session.session_id in session.username
    assert session.proxy_id == "dataimpulse-slot-2"
    assert session.port == "823"
    assert session.host == "gw.dataimpulse.com"


def test_new_session_mints_a_distinct_sessid_each_call() -> None:
    provider = DataImpulseProvider(_config())
    sessids = {provider.new_session(slot_id=1).session_id for _ in range(100)}

    assert len(sessids) == 100
