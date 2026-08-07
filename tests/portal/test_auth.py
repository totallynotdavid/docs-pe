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

    from litestar import Litestar
    from portal.repository.auth import PostgresAuthRepository
    from portal.repository.teams import PostgresTeamRepository


async def test_postgresql_login_is_allowed_without_recent_failures(
    auth_repository: PostgresAuthRepository,
) -> None:
    now = datetime.now(UTC)

    assert await auth_repository.login_allowed(
        "persona@example.test",
        "127.0.0.1",
        now,
    )


async def test_login_page_and_generic_failure(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        page = client.get("/login")

        assert "Cerrar sesión" not in page.text
        assert "Iniciar sesión" in page.text

        unknown = login(client, "nadie@osiptel.test")
        wrong = login(client, "admin@osiptel.test", "otra-clave-larga")

        assert unknown.headers["location"] == "/login?error=1"
        assert wrong.headers["location"] == "/login?error=1"


async def test_login_rejects_bad_origin(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        page = client.get("/login")

        response = client.post(
            "/login",
            data={
                "email": "nadie@osiptel.test",
                "password": PASSWORD,
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": "https://evil.example"},
        )

        assert response.status_code == 403


async def test_successful_login_sets_session_cookie(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        response = login(client, "admin@osiptel.test")
        cookie = response.headers["set-cookie"]

        assert response.status_code == 303
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
        assert client.get("/admin").status_code == 200


async def test_logout_requires_valid_origin(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "admin@osiptel.test").status_code == 303

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

        assert missing_origin.status_code == 403
        assert bad_origin.status_code == 403


async def test_logout_ends_the_session(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "admin@osiptel.test").status_code == 303

        logout = client.post(
            "/logout",
            data={"csrf_token": session_csrf(client)},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert logout.status_code == 303
        assert client.get("/", follow_redirects=False).status_code == 303


async def test_forged_csrf_token_is_rejected(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
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
