from __future__ import annotations

import pytest

from fetch.proxy.dataimpulse import (
    DataImpulseConfig,
    DataImpulseProvider,
    load_dataimpulse_config,
)


_ENV_FILE = "/nonexistent/does-not-exist.env"

_ENV_VARS = (
    "DATAIMPULSE_USER",
    "DATAIMPULSE_PASS",
    "DATAIMPULSE_COUNTRY",
    "DATAIMPULSE_SESSTTL",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _set_minimal_valid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAIMPULSE_USER", "user")
    monkeypatch.setenv("DATAIMPULSE_PASS", "pass")
    monkeypatch.setenv("DATAIMPULSE_COUNTRY", "pe")


def test_missing_user_or_pass_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAIMPULSE_COUNTRY", "pe")
    with pytest.raises(RuntimeError, match="DATAIMPULSE_USER or DATAIMPULSE_PASS"):
        load_dataimpulse_config(env_file=_ENV_FILE)


def test_empty_country_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAIMPULSE_USER", "user")
    monkeypatch.setenv("DATAIMPULSE_PASS", "pass")
    with pytest.raises(RuntimeError, match="DATAIMPULSE_COUNTRY must not be empty"):
        load_dataimpulse_config(env_file=_ENV_FILE)


def test_sessttl_below_one_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_minimal_valid_env(monkeypatch)
    monkeypatch.setenv("DATAIMPULSE_SESSTTL", "0")
    with pytest.raises(RuntimeError, match="DATAIMPULSE_SESSTTL must be >= 1"):
        load_dataimpulse_config(env_file=_ENV_FILE)


def test_country_is_lowercased_and_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATAIMPULSE_USER", "user")
    monkeypatch.setenv("DATAIMPULSE_PASS", "pass")
    monkeypatch.setenv("DATAIMPULSE_COUNTRY", "  PE  ")
    config = load_dataimpulse_config(env_file=_ENV_FILE)
    assert config.country == "pe"


def test_happy_path_defaults_sessttl(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_minimal_valid_env(monkeypatch)
    config = load_dataimpulse_config(env_file=_ENV_FILE)
    assert config == DataImpulseConfig(
        user="user", password="pass", country="pe", sessttl=3, host="gw.dataimpulse.com"
    )


def _config() -> DataImpulseConfig:
    return DataImpulseConfig(
        user="user", password="pass", country="pe", sessttl=5, host="gw.dataimpulse.com"
    )


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
    # 100 calls: a seeded or deterministic RNG would collide before this.
    sessids = {provider.new_session(slot_id=1).session_id for _ in range(100)}
    assert len(sessids) == 100
