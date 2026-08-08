from __future__ import annotations

from pathlib import Path

import pytest

from portal.settings import PortalSettings


_REQUIRED_ENV = {
    "PORTAL_DATABASE_DSN": "postgresql://postgres@127.0.0.1:5432/postgres",
    "PORTAL_ENVIRONMENT": "production",
    "PORTAL_PUBLIC_ORIGIN": "https://portal.osiptel.test",
    "PORTAL_TLS_TERMINATED_UPSTREAM": "true",
    "PORTAL_MASTER_KEY_FILE": "/run/secrets/portal-master-key",
}

_PRODUCTION_ENV = {
    **_REQUIRED_ENV,
    "PORTAL_TURNSTILE_SITE_KEY": "0x0000",
    "PORTAL_TURNSTILE_SECRET": "0x1111",
    "PORTAL_WORKER_API_HOST": "100.64.0.2",
}


def _set_environment(
    monkeypatch: pytest.MonkeyPatch,
    values: dict[str, str],
    **overrides: str,
) -> None:
    for name, value in {**values, **overrides}.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("missing", sorted(_REQUIRED_ENV))
def test_missing_required_variable_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    _set_environment(monkeypatch, _REQUIRED_ENV)
    monkeypatch.delenv(missing)

    with pytest.raises(RuntimeError, match=missing):
        PortalSettings.from_environment()


def test_tls_flag_rejects_anything_other_than_true_or_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(
        monkeypatch,
        _REQUIRED_ENV,
        PORTAL_TLS_TERMINATED_UPSTREAM="yes",
    )

    with pytest.raises(RuntimeError, match="PORTAL_TLS_TERMINATED_UPSTREAM"):
        PortalSettings.from_environment()


def test_fully_specified_environment_is_never_silently_defaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(monkeypatch, _PRODUCTION_ENV)

    settings = PortalSettings.from_environment()
    settings.validate()

    assert settings.environment == "production"
    assert settings.public_origin == "https://portal.osiptel.test"
    assert settings.serves_https is True
    assert settings.session_cookie == "__Host-portal-id"
    assert settings.master_key_file == Path("/run/secrets/portal-master-key")
    assert settings.worker_api_host == "100.64.0.2"


def test_https_and_secure_cookies_follow_the_origin_not_the_environment() -> None:
    development = PortalSettings(
        database_dsn="postgresql://x",
        public_origin="http://localhost:8000",
    )
    deployed = PortalSettings(
        database_dsn="postgresql://x",
        public_origin="https://portal.osiptel.test",
    )

    assert development.serves_https is False
    assert development.session_cookie == "portal-id"
    assert deployed.serves_https is True
    assert deployed.session_cookie == "__Host-portal-id"


def test_validate_rejects_an_origin_without_hostname() -> None:
    settings = PortalSettings(
        database_dsn="postgresql://x",
        public_origin="https:///sin-host",
    )

    with pytest.raises(RuntimeError, match="hostname"):
        settings.validate()


@pytest.mark.parametrize(
    "dropped",
    ["PORTAL_TURNSTILE_SITE_KEY", "PORTAL_TURNSTILE_SECRET"],
)
def test_production_refuses_every_development_convenience(
    monkeypatch: pytest.MonkeyPatch,
    dropped: str,
) -> None:
    _set_environment(monkeypatch, _PRODUCTION_ENV)
    monkeypatch.delenv(dropped)

    with pytest.raises(RuntimeError, match=dropped):
        PortalSettings.from_environment().validate()


def test_production_refuses_a_plain_http_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(
        monkeypatch,
        _PRODUCTION_ENV,
        PORTAL_PUBLIC_ORIGIN="http://portal.osiptel.test",
    )

    with pytest.raises(RuntimeError, match="must use https"):
        PortalSettings.from_environment().validate()


def test_an_origin_with_a_port_answers_to_both_host_forms() -> None:
    # Litestar matches the Host header verbatim, so a development origin on a
    # port has to allow the header the browser actually sends.
    local = PortalSettings(
        database_dsn="postgresql://x",
        public_origin="http://localhost:8000",
    )
    deployed = PortalSettings(
        database_dsn="postgresql://x",
        public_origin="https://portal.osiptel.test",
    )

    assert local.allowed_hosts == ("localhost:8000", "localhost")
    assert local.hostname == "localhost"
    assert deployed.allowed_hosts == ("portal.osiptel.test",)
