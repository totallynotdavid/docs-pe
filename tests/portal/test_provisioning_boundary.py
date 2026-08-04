from __future__ import annotations

import re

from typing import TYPE_CHECKING

import pytest

from fastapi.testclient import TestClient
from portal.domain.errors import ProvisioningError, Reason
from portal.domain.models import TeamRole
from portal.security import hash_password

from tests.portal.conftest import seed_user


if TYPE_CHECKING:
    import asyncpg

    from fastapi import FastAPI
    from portal.application.provisioning import ProvisioningService
    from portal.repository.teams import PostgresTeamRepository


ORIGIN = "http://testserver"
PASSWORD = "una-clave-larga-y-segura"


def _csrf(page: str) -> str:
    found = re.search(r'name="csrf_token" value="([^"]+)"', page)
    assert found is not None
    return found.group(1)


def _login(client: TestClient, email: str) -> None:
    page = client.get("/login")
    response = client.post(
        "/login",
        data={
            "email": email,
            "password": PASSWORD,
            "csrf_token": _csrf(page.text),
        },
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _client(app: FastAPI) -> TestClient:
    return TestClient(app)


async def _user(
    pool: asyncpg.Pool,
    email: str,
    *,
    is_site_admin: bool = False,
) -> str:
    user_id = await seed_user(pool, email=email)
    await pool.execute(
        "UPDATE portal_users SET password_hash = $2, is_site_admin = $3 WHERE id = $1",
        user_id,
        hash_password(PASSWORD),
        is_site_admin,
    )
    return email


async def test_first_team_setup_is_the_only_empty_installation_path(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: FastAPI,
) -> None:
    admin_email = await _user(pool, "admin@osiptel.test", is_site_admin=True)
    admin_id = await pool.fetchval(
        "SELECT id FROM portal_users WHERE email = $1",
        admin_email,
    )

    with _client(app) as client:
        _login(client, admin_email)

        assert client.get("/", follow_redirects=False).headers["location"] == "/inicio"

        page = client.get("/inicio")
        assert "UUID" not in page.text
        assert "persona líder" not in page.text

        response = client.post(
            "/inicio",
            data={
                "name": "Equipo Lima",
                "slug": "equipo-lima",
                "csrf_token": _csrf(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"].endswith("/ajustes/proxy")
        assert "config_ciphertext" not in client.get(response.headers["location"]).text

    team = await team_repository.team_by_slug("equipo-lima")
    assert team is not None
    assert await team_repository.role_for(admin_id, team.id) is TeamRole.TEAM_LEADER


async def test_site_and_team_settings_use_email_selectors_and_keep_members_limited(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: FastAPI,
) -> None:
    admin_email = await _user(pool, "admin@osiptel.test", is_site_admin=True)
    leader_email = await _user(pool, "lider@osiptel.test")
    member_email = await _user(pool, "miembro@osiptel.test")

    admin_id = await pool.fetchval(
        "SELECT id FROM portal_users WHERE email = $1",
        admin_email,
    )
    await team_repository.create_first_team("inicial", "Inicial", admin_id)

    with _client(app) as admin_client:
        _login(admin_client, admin_email)

        page = admin_client.get("/administracion/equipos")
        assert "leader_id" not in page.text
        assert "ID de la persona" not in page.text

        response = admin_client.post(
            "/administracion/equipos",
            data={
                "name": "Consultas Norte",
                "slug": "consultas-norte",
                "leader_email": leader_email,
                "csrf_token": _csrf(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert response.status_code == 303

        team_url = response.headers["location"].removesuffix("/ajustes")

        assert (
            admin_client.post(
                "/administracion/miembros",
                headers={"Origin": ORIGIN},
            ).status_code
            == 404
        )

    with _client(app) as leader_client:
        _login(leader_client, leader_email)

        page = leader_client.get(f"{team_url}/ajustes/miembros")

        response = leader_client.post(
            f"{team_url}/ajustes/miembros",
            data={
                "email": member_email,
                "role": "team_member",
                "csrf_token": _csrf(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert leader_client.get(f"{team_url}/credenciales").status_code == 404

    with _client(app) as member_client:
        _login(member_client, member_email)

        assert member_client.get(f"{team_url}/buscar").status_code == 200
        assert member_client.get(f"{team_url}/ajustes").status_code == 403
        assert member_client.get(f"{team_url}/procesos/nuevo").status_code == 403


async def test_ensure_first_team_creates_once_and_verifies_on_rerun(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    provisioning: ProvisioningService,
) -> None:
    admin_email = await _user(pool, "bootstrap@osiptel.test", is_site_admin=True)
    admin_id = await pool.fetchval(
        "SELECT id FROM portal_users WHERE email = $1",
        admin_email,
    )

    created = await provisioning.ensure_first_team(
        admin_id,
        name="Equipo Inicial",
        slug="equipo-inicial",
    )

    assert created.created is True
    assert (
        await team_repository.role_for(admin_id, created.team.id)
        is TeamRole.TEAM_LEADER
    )

    rerun = await provisioning.ensure_first_team(
        admin_id,
        name="Equipo Inicial",
        slug="equipo-inicial",
    )

    assert rerun.created is False
    assert rerun.team.id == created.team.id
    assert (
        await team_repository.role_for(admin_id, rerun.team.id) is TeamRole.TEAM_LEADER
    )


async def test_ensure_first_team_rejects_a_mismatched_rerun(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    provisioning: ProvisioningService,
) -> None:
    admin_email = await _user(pool, "bootstrap@osiptel.test", is_site_admin=True)
    admin_id = await pool.fetchval(
        "SELECT id FROM portal_users WHERE email = $1",
        admin_email,
    )

    await team_repository.create_first_team(
        "equipo-inicial",
        "Equipo Inicial",
        admin_id,
    )

    with pytest.raises(ProvisioningError) as excinfo:
        await provisioning.ensure_first_team(
            admin_id,
            name="Otro Equipo",
            slug="otro-equipo",
        )

    assert excinfo.value.reason is Reason.INITIAL_TEAM_MISMATCH
