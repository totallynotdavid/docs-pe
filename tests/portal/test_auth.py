from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tests.portal.conftest import (
    ORIGIN,
    PASSWORD,
    build_experience,
    csrf_token,
    login,
    session_csrf,
    sync_client,
)


if TYPE_CHECKING:
    import asyncpg

    from fastapi import FastAPI
    from portal.repository.auth import PostgresAuthRepository
    from portal.repository.teams import PostgresTeamRepository


async def test_postgresql_login_limit_uses_a_timestamp_window(
    auth_repository: PostgresAuthRepository,
) -> None:
    now = datetime.now(UTC)

    assert await auth_repository.login_allowed(
        "persona@example.test",
        "127.0.0.1",
        now,
    )


async def test_login_csrf_cookie_rotation_and_generic_failure(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: FastAPI,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        page = client.get("/login")

        assert 'class="barra-superior"' not in page.text
        assert 'class="acceso__marca"' in page.text

        bad_origin = client.post(
            "/login",
            data={
                "email": "nadie@osiptel.test",
                "password": PASSWORD,
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": "https://evil.example"},
        )

        assert bad_origin.status_code == 403

        unknown = login(client, "nadie@osiptel.test")
        wrong = login(client, "admin@osiptel.test", "otra-clave-larga")

        assert unknown.headers["location"] == "/login?error=1"
        assert wrong.headers["location"] == "/login?error=1"

        admin_login = login(client, "admin@osiptel.test")
        cookie = admin_login.headers["set-cookie"]

        assert admin_login.status_code == 303
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert client.get("/administracion").status_code == 200

        csrf = session_csrf(client)

        missing_origin = client.post(
            "/logout",
            data={"csrf_token": csrf},
        )
        bad_origin = client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={"Origin": "https://evil.example"},
        )
        logout = client.post(
            "/logout",
            data={"csrf_token": csrf},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert missing_origin.status_code == 403
        assert bad_origin.status_code == 403
        assert logout.status_code == 303
        assert client.get("/", follow_redirects=False).status_code == 303


async def test_a_forged_csrf_token_on_a_protected_route_is_rejected(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: FastAPI,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "admin@osiptel.test").status_code == 303

        forged = client.post(
            "/logout",
            data={"csrf_token": "no-es-el-token-de-la-sesion"},
            headers={"Origin": ORIGIN},
        )

        assert forged.status_code == 403
        assert "la verificación CSRF no es válida" in forged.text
