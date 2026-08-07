from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from portal.domain.errors import ProvisioningError, Reason
from portal.domain.models import TeamRole
from portal.security import hash_password

from tests.portal.conftest import (
    ORIGIN,
    PASSWORD,
    csrf_token,
    login,
    seed_user,
    sync_client,
)


if TYPE_CHECKING:
    import asyncpg

    from litestar import Litestar
    from portal.application.provisioning import ProvisioningService
    from portal.repository.teams import PostgresTeamRepository


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
    app: Litestar,
) -> None:
    admin_email = await _user(pool, "admin@osiptel.test", is_site_admin=True)
    admin_id = await pool.fetchval(
        "SELECT id FROM portal_users WHERE email = $1",
        admin_email,
    )

    with sync_client(app) as client:
        assert login(client, admin_email).status_code == 303

        assert client.get("/", follow_redirects=False).headers["location"] == "/setup"

        page = client.get("/setup")
        assert "UUID" not in page.text
        assert "persona líder" not in page.text

        response = client.post(
            "/setup",
            data={
                "name": "Equipo Lima",
                "slug": "equipo-lima",
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert response.headers["location"].endswith("/settings/proxy")
        assert "config_ciphertext" not in client.get(response.headers["location"]).text

    team = await team_repository.team_by_slug("equipo-lima")
    assert team is not None
    assert await team_repository.role_for(admin_id, team.id) is TeamRole.TEAM_LEADER


async def test_site_and_team_settings_use_email_selectors_and_keep_members_limited(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    admin_email = await _user(pool, "admin@osiptel.test", is_site_admin=True)
    leader_email = await _user(pool, "lider@osiptel.test")
    member_email = await _user(pool, "miembro@osiptel.test")

    admin_id = await pool.fetchval(
        "SELECT id FROM portal_users WHERE email = $1",
        admin_email,
    )
    await team_repository.create_first_team("inicial", "Inicial", admin_id)

    with sync_client(app) as admin_client:
        assert login(admin_client, admin_email).status_code == 303

        page = admin_client.get("/admin/teams")
        assert "leader_id" not in page.text
        assert "ID de la persona" not in page.text

        response = admin_client.post(
            "/admin/teams",
            data={
                "name": "Consultas Norte",
                "slug": "consultas-norte",
                "leader_email": leader_email,
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert response.status_code == 303

        team_url = response.headers["location"].removesuffix("/settings")

        assert (
            admin_client.post(
                "/admin/members",
                headers={"Origin": ORIGIN},
            ).status_code
            == 404
        )

    with sync_client(app) as leader_client:
        assert login(leader_client, leader_email).status_code == 303

        page = leader_client.get(f"{team_url}/settings/members")

        response = leader_client.post(
            f"{team_url}/settings/members",
            data={
                "email": member_email,
                "role": "team_member",
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert leader_client.get(f"{team_url}/credentials").status_code == 404

    with sync_client(app) as member_client:
        assert login(member_client, member_email).status_code == 303

        assert member_client.get(f"{team_url}/search").status_code == 200
        assert member_client.get(f"{team_url}/settings").status_code == 403
        assert member_client.get(f"{team_url}/jobs/new").status_code == 403


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
