"""PortalSettings.from_environment fails fast on missing or malformed configuration."""

from __future__ import annotations

import pytest

from portal.settings import PortalSettings


_REQUIRED_ENV = {
    "PORTAL_DATABASE_DSN": "postgresql://postgres@127.0.0.1:5432/postgres",
    "PORTAL_WORKER_BOOTSTRAP_TOKEN": "ficha-de-prueba",
    "PORTAL_ENVIRONMENT": "production",
    "PORTAL_PUBLIC_ORIGIN": "https://portal.osiptel.test",
    "PORTAL_COOKIE_SECURE": "true",
    "PORTAL_TLS_TERMINATED_UPSTREAM": "true",
}


def _set_environment(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    for name, value in {**_REQUIRED_ENV, **overrides}.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("missing", sorted(_REQUIRED_ENV))
def test_a_missing_required_variable_fails_fast(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    _set_environment(monkeypatch)
    monkeypatch.delenv(missing)

    with pytest.raises(RuntimeError, match=missing):
        PortalSettings.from_environment()


def test_cookie_secure_rejects_anything_other_than_true_or_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(monkeypatch, PORTAL_COOKIE_SECURE="yes")

    with pytest.raises(RuntimeError, match="PORTAL_COOKIE_SECURE"):
        PortalSettings.from_environment()


def test_a_fully_specified_environment_is_never_silently_defaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_environment(monkeypatch)

    settings = PortalSettings.from_environment()

    assert settings.environment == "production"
    assert settings.public_origin == "https://portal.osiptel.test"
    assert settings.cookie_secure is True
    assert settings.worker_bootstrap_token == "ficha-de-prueba"
    assert settings.tls_terminated_upstream is True


def test_validate_rejects_a_production_origin_without_a_hostname() -> None:
    settings = PortalSettings(
        database_dsn="postgresql://x",
        worker_bootstrap_token="token",
        environment="production",
        public_origin="https:///sin-host",
        cookie_secure=True,
    )

    with pytest.raises(RuntimeError, match="hostname"):
        settings.validate()


def test_validate_requires_a_worker_bootstrap_token() -> None:
    settings = PortalSettings(database_dsn="postgresql://x", worker_bootstrap_token="")

    with pytest.raises(RuntimeError, match="PORTAL_WORKER_BOOTSTRAP_TOKEN"):
        settings.validate()
