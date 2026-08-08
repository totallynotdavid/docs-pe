from __future__ import annotations

import json

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pyotp

from portal.application.access import STEP_UP_WINDOW
from portal.application.sessions import SESSION_IDLE
from portal.security import token_hash

from tests.portal.conftest import (
    ORIGIN,
    TOTP_SECRET,
    build_experience,
    csrf_token,
    login,
    sync_client,
)


if TYPE_CHECKING:
    import asyncpg

    from litestar import Litestar
    from litestar.testing import TestClient
    from portal.ephemeral import EphemeralStore
    from portal.repository.teams import PostgresTeamRepository
    from portal.settings import PortalSettings


async def _age_step_up(
    client: TestClient,
    store: EphemeralStore,
    settings: PortalSettings,
    *,
    by: timedelta,
) -> None:
    """Push the session's second-factor proof back in time, as if it had been
    set at login `by` ago rather than moments ago."""
    token = client.cookies[settings.session_cookie]
    key = f"session:{token_hash(token)}"
    record = json.loads(await store.read(key) or "{}")

    record["mfa_verified_at"] = (datetime.now(UTC) - by).timestamp()

    await store.replace(key, json.dumps(record), SESSION_IDLE)


async def test_a_fresh_login_can_create_a_user_without_reverifying(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "admin@osiptel.test").status_code == 303

        page = client.get("/admin/users")
        response = client.post(
            "/admin/users",
            data={
                "email": "nueva@osiptel.test",
                "password": "una-clave-larga-y-nueva",
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"] == "/admin/users"


async def test_a_stale_second_factor_redirects_to_step_up_and_loses_no_access(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
    store: EphemeralStore,
    settings: PortalSettings,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "admin@osiptel.test").status_code == 303
        await _age_step_up(
            client,
            store,
            settings,
            by=STEP_UP_WINDOW + timedelta(minutes=1),
        )

        page = client.get("/admin/users")
        stale = client.post(
            "/admin/users",
            data={
                "email": "otra@osiptel.test",
                "password": "una-clave-larga-tambien",
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert stale.status_code == 303
        assert stale.headers["location"] == "/step-up?next_path=/admin/users"

        reverify_page = client.get(stale.headers["location"])
        verified = client.post(
            "/step-up",
            data={
                "code": pyotp.TOTP(TOTP_SECRET).now(),
                "next_path": "/admin/users",
                "csrf_token": csrf_token(reverify_page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert verified.status_code == 303
        assert verified.headers["location"] == "/admin/users"

        # The reverified session's proof is fresh again, so the same action
        # this admin was denied a moment ago now goes through.
        page = client.get("/admin/users")
        retried = client.post(
            "/admin/users",
            data={
                "email": "otra@osiptel.test",
                "password": "una-clave-larga-tambien",
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert retried.status_code == 303
        assert retried.headers["location"] == "/admin/users"


async def test_a_wrong_code_at_step_up_stays_stale(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
    store: EphemeralStore,
    settings: PortalSettings,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "admin@osiptel.test").status_code == 303
        await _age_step_up(
            client,
            store,
            settings,
            by=STEP_UP_WINDOW + timedelta(minutes=1),
        )

        page = client.get("/step-up")
        rejected = client.post(
            "/step-up",
            data={
                "code": "000000",
                "next_path": "/admin/users",
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert rejected.status_code == 303
        assert rejected.headers["location"].startswith("/step-up?next_path=")
        assert rejected.headers["location"].endswith("&error=1")

        still_stale = client.post(
            "/admin/teams",
            data={
                "name": "Equipo Nuevo",
                "slug": "equipo-nuevo",
                "leader_email": "admin@osiptel.test",
                "csrf_token": csrf_token(client.get("/admin/teams").text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert still_stale.status_code == 303
        assert still_stale.headers["location"] == "/step-up?next_path=/admin/teams"


async def test_step_up_next_path_cannot_redirect_off_site(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "admin@osiptel.test").status_code == 303

        page = client.get("/step-up?next_path=https://evil.example")
        verified = client.post(
            "/step-up",
            data={
                "code": pyotp.TOTP(TOTP_SECRET).now(),
                "next_path": "https://evil.example",
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert verified.status_code == 303
        assert verified.headers["location"] == "/"
