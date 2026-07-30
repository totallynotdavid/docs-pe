from __future__ import annotations

import asyncio
import re

from fastapi.testclient import TestClient
from portal.domain.models import TeamRole
from portal.repository.memory import InMemoryPortalRepository
from portal.web.app import PortalSettings, create_app
from portal.web.security import hash_password


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
        data={"email": email, "password": PASSWORD, "csrf_token": _csrf(page.text)},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _client(repository: InMemoryPortalRepository) -> TestClient:
    return TestClient(
        create_app(PortalSettings("", public_origin=ORIGIN), repository=repository)
    )


def test_first_team_setup_is_the_only_empty_installation_path() -> None:
    repository = InMemoryPortalRepository()
    admin = repository.add_user(
        "admin@osiptel.test", hash_password(PASSWORD), is_site_admin=True
    )
    with _client(repository) as client:
        _login(client, admin.email)
        assert client.get("/", follow_redirects=False).headers["location"] == "/inicio"
        first = client.get("/inicio")
        assert "UUID" not in first.text and "persona líder" not in first.text
        created = client.post(
            "/inicio",
            data={
                "name": "Equipo Lima",
                "slug": "equipo-lima",
                "csrf_token": _csrf(first.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        assert created.status_code == 303
        assert created.headers["location"].endswith("/ajustes/proxy")
        assert "config_ciphertext" not in client.get(created.headers["location"]).text

    team = asyncio.run(repository.team_by_slug("equipo-lima"))
    assert team is not None
    assert asyncio.run(repository.role_for(admin.id, team.id)) is TeamRole.TEAM_LEADER


def test_site_and_team_settings_use_email_selectors_and_keep_members_limited() -> None:
    repository = InMemoryPortalRepository()
    admin = repository.add_user(
        "admin@osiptel.test", hash_password(PASSWORD), is_site_admin=True
    )
    leader = repository.add_user("lider@osiptel.test", hash_password(PASSWORD))
    member = repository.add_user("miembro@osiptel.test", hash_password(PASSWORD))
    asyncio.run(repository.create_first_team("inicial", "Inicial", admin.id))

    with _client(repository) as admin_client:
        _login(admin_client, admin.email)
        teams = admin_client.get("/administracion/equipos")
        assert "leader_id" not in teams.text and "ID de la persona" not in teams.text
        created = admin_client.post(
            "/administracion/equipos",
            data={
                "name": "Consultas Norte",
                "slug": "consultas-norte",
                "leader_email": leader.email,
                "csrf_token": _csrf(teams.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        assert created.status_code == 303
        team_url = created.headers["location"].removesuffix("/ajustes")
        assert (
            admin_client.post(
                "/administracion/miembros", headers={"Origin": ORIGIN}
            ).status_code
            == 404
        )

    team_id = team_url.rsplit("/", 1)[1]
    with _client(repository) as leader_client:
        _login(leader_client, leader.email)
        page = leader_client.get(f"{team_url}/ajustes/miembros")
        added = leader_client.post(
            f"{team_url}/ajustes/miembros",
            data={
                "email": member.email,
                "role": "team_member",
                "csrf_token": _csrf(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        assert added.status_code == 303
        assert leader_client.get(f"{team_url}/credenciales").status_code == 404

    with _client(repository) as member_client:
        _login(member_client, member.email)
        assert member_client.get(f"{team_url}/buscar").status_code == 200
        assert member_client.get(f"{team_url}/ajustes").status_code == 403
        assert member_client.get(f"{team_url}/procesos/nuevo").status_code == 403
    assert team_id
