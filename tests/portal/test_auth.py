from __future__ import annotations

from typing import TYPE_CHECKING

import pyotp

from portal.application.throttle import LOGIN_ATTEMPT_LIMIT

from tests.portal.conftest import (
    ORIGIN,
    PASSWORD,
    RECOVERY_CODE,
    TOTP_SECRET,
    build_experience,
    csrf_token,
    login,
    session_csrf,
    submit_mfa_code,
    sync_client,
)


if TYPE_CHECKING:
    import asyncpg
    import httpx

    from litestar import Litestar
    from litestar.testing import TestClient
    from portal.repository.teams import PostgresTeamRepository


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


async def test_fetch_metadata_overrides_a_missing_origin(
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
            headers={"Sec-Fetch-Site": "same-origin"},
            follow_redirects=False,
        )

        assert response.status_code == 303


async def test_fetch_metadata_overrides_a_matching_origin(
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
            headers={"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site"},
        )

        assert response.status_code == 403


async def test_a_login_csrf_token_cannot_be_replayed(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        token = csrf_token(client.get("/login").text)
        form = {
            "email": "admin@osiptel.test",
            "password": PASSWORD,
            "csrf_token": token,
        }

        first = client.post(
            "/login",
            data=form,
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        replayed = client.post(
            "/login",
            data=form,
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert first.headers["location"] == "/login/mfa"
        assert replayed.headers["location"] == "/login?error=1"


async def test_a_site_admin_must_pass_the_second_factor(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        page = client.get("/login")

        challenged = client.post(
            "/login",
            data={
                "email": "admin@osiptel.test",
                "password": PASSWORD,
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert challenged.headers["location"] == "/login/mfa"

        # The password alone does not open the session.
        assert client.get("/", follow_redirects=False).status_code == 303

        verified = submit_mfa_code(client, pyotp.TOTP(TOTP_SECRET).now())

        assert verified.headers["location"] == "/"
        assert client.get("/admin").status_code == 200


async def test_a_wrong_code_sends_the_browser_back_to_the_password_step(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        page = client.get("/login")

        client.post(
            "/login",
            data={
                "email": "admin@osiptel.test",
                "password": PASSWORD,
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        rejected = submit_mfa_code(client, "000000")

        assert rejected.headers["location"] == "/login?error=1"
        assert (
            client.get("/login/mfa", follow_redirects=False).headers["location"]
            == "/login"
        )


async def test_a_recovery_code_works_once(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "admin@osiptel.test").status_code == 303

        client.post(
            "/logout",
            data={"csrf_token": session_csrf(client)},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        spent = _login_with_code(client, RECOVERY_CODE)
        replayed = _login_with_code(client, RECOVERY_CODE)

        assert spent.headers["location"] == "/"
        assert replayed.headers["location"] == "/login?error=1"


async def test_repeated_failures_soft_lock_the_account(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        for _ in range(LOGIN_ATTEMPT_LIMIT + 1):
            login(client, "admin@osiptel.test", "clave-equivocada-larga")

        # The correct password no longer helps while the window is open.
        assert login(client, "admin@osiptel.test").headers["location"] == (
            "/login?error=1"
        )


async def test_successful_login_sets_a_host_prefixed_session_cookie(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        response = login(client, "admin@osiptel.test")
        cookie = response.headers["set-cookie"]

        assert response.status_code == 303
        assert "__Host-portal-id" in cookie
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "samesite=strict" in cookie.lower()
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


async def test_a_denied_request_is_written_to_the_audit_log(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    experience = await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "miembro@osiptel.test").status_code == 303

        denied = client.get(f"/teams/{experience.team_id}/settings/proxy")

        assert denied.status_code == 403

    recorded = await pool.fetchrow(
        """
        SELECT actor_id, metadata
          FROM portal_audit_log
         WHERE action = 'permission.denied'
        """
    )

    assert recorded is not None
    assert recorded["actor_id"] == experience.member_id


def _login_with_code(client: TestClient, code: str) -> httpx.Response:
    page = client.get("/login")

    client.post(
        "/login",
        data={
            "email": "admin@osiptel.test",
            "password": PASSWORD,
            "csrf_token": csrf_token(page.text),
        },
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )

    return submit_mfa_code(client, code)
