"""The portal's HTTP surface, driven end to end against real PostgreSQL."""

from __future__ import annotations

import re

from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from fastapi.testclient import TestClient
from portal.domain.models import TeamRole
from portal.security import hash_password

from tests.portal.conftest import object_reference, seed_team, seed_user


if TYPE_CHECKING:
    import asyncpg

    from fastapi import FastAPI
    from portal.repository.postgres import PostgresPortalRepository


ORIGIN = "http://testserver"
PASSWORD = "una-clave-larga-y-segura"


def _csrf(response_text: str) -> str:
    found = re.search(r'name="csrf_token" value="([^"]+)"', response_text)
    assert found is not None
    return found.group(1)


def _login(client: TestClient, email: str, password: str = PASSWORD):
    page = client.get("/login")
    return client.post(
        "/login",
        data={"email": email, "password": password, "csrf_token": _csrf(page.text)},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )


def _session_csrf(client: TestClient) -> str:
    return _csrf(client.get("/").text)


@dataclass(frozen=True)
class Experience:
    """One installation with the four roles the pages distinguish between."""

    admin_id: UUID
    leader_id: UUID
    member_id: UUID
    team_id: UUID
    credential_id: UUID


async def _experience(
    pool: asyncpg.Pool, repository: PostgresPortalRepository
) -> Experience:
    hashed = hash_password(PASSWORD)
    team = await seed_team(pool, password_hash=hashed)
    other = await seed_team(pool, password_hash=hashed)
    admin_id = await seed_user(pool, email="admin@osiptel.test")
    member_id = await seed_user(pool, email="miembro@osiptel.test")
    await pool.execute(
        """
        UPDATE portal_users
           SET password_hash = $2,
               is_site_admin = (id = $1),
               email = CASE id WHEN $3 THEN 'lider@osiptel.test'
                               WHEN $4 THEN 'otro@osiptel.test'
                               ELSE email END
         WHERE id = ANY($5::uuid[])
        """,
        admin_id,
        hashed,
        team.actor_id,
        other.actor_id,
        [admin_id, member_id, team.actor_id, other.actor_id],
    )
    await repository.add_member(team.team_id, member_id, TeamRole.TEAM_MEMBER)
    return Experience(
        admin_id, team.actor_id, member_id, team.team_id, team.credential_id
    )


def _client(app: FastAPI) -> TestClient:
    return TestClient(app)


def _submit_job(client: TestClient, team_id: UUID, credential_id: UUID, documents: str):
    response = client.post(
        f"/equipos/{team_id}/procesos",
        data={
            "credential_version_id": str(credential_id),
            "sources": "osiptel",
            "csrf_token": _session_csrf(client),
        },
        files={"input_file": ("registros.csv", documents.encode(), "text/csv")},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return UUID(response.headers["location"].rsplit("/", 1)[1])


def _submit_csv(client: TestClient, team_id: UUID, credential_id: UUID):
    response = client.post(
        f"/equipos/{team_id}/procesos",
        data={
            "credential_version_id": str(credential_id),
            "sources": "osiptel",
            "csrf_token": _session_csrf(client),
        },
        files={
            "input_file": ("barranca.csv", b"10412345678\n10412345679\n", "text/csv")
        },
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return UUID(response.headers["location"].rsplit("/", 1)[1])


async def test_login_csrf_cookie_rotation_and_generic_failure(
    pool: asyncpg.Pool, repository: PostgresPortalRepository, app: FastAPI
) -> None:
    await _experience(pool, repository)
    with _client(app) as client:
        page = client.get("/login")
        assert 'class="barra-superior"' not in page.text
        assert 'class="acceso__marca"' in page.text
        bad_origin = client.post(
            "/login",
            data={
                "email": "nadie@osiptel.test",
                "password": PASSWORD,
                "csrf_token": _csrf(page.text),
            },
            headers={"Origin": "https://evil.example"},
        )
        assert bad_origin.status_code == 403

        unknown = _login(client, "nadie@osiptel.test")
        wrong = _login(client, "admin@osiptel.test", "otra-clave-larga")
        assert (
            unknown.headers["location"] == wrong.headers["location"] == "/login?error=1"
        )

        login = _login(client, "admin@osiptel.test")
        cookie = login.headers["set-cookie"]
        assert login.status_code == 303
        assert "HttpOnly" in cookie and "SameSite=lax" in cookie
        assert client.get("/administracion").status_code == 200

        csrf = _session_csrf(client)
        assert client.post("/logout", data={"csrf_token": csrf}).status_code == 403
        assert (
            client.post(
                "/logout",
                data={"csrf_token": csrf},
                headers={"Origin": "https://evil.example"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/logout",
                data={"csrf_token": csrf},
                headers={"Origin": ORIGIN},
                follow_redirects=False,
            ).status_code
            == 303
        )
        assert client.get("/", follow_redirects=False).status_code == 303


async def test_new_job_makes_source_outcomes_visible_without_exposing_setup_details(
    pool: asyncpg.Pool, repository: PostgresPortalRepository, app: FastAPI
) -> None:
    team_id = (await _experience(pool, repository)).team_id
    with _client(app) as client:
        assert _login(client, "lider@osiptel.test").status_code == 303
        page = client.get(f"/equipos/{team_id}/procesos/nuevo")

    assert page.status_code == 200
    assert "DNI y nombre" in page.text
    assert "Para RUC que empiezan en 10" in page.text
    assert "Recibirás: DNI y nombre de la persona." in page.text
    assert "Así se verá el resultado" in page.text
    assert "DNI" in page.text and "Nombre" in page.text
    assert re.search(r'value="sunat"\s+checked', page.text)
    assert "Nombre de la consulta" not in page.text
    assert "versión 1" not in page.text


async def test_roles_cross_team_isolation_submission_and_terminal_rendering(
    pool: asyncpg.Pool, repository: PostgresPortalRepository, app: FastAPI
) -> None:
    people = await _experience(pool, repository)
    leader_id, member_id = people.leader_id, people.member_id
    team_id, credential_id = people.team_id, people.credential_id
    with _client(app) as member_client:
        assert _login(member_client, "miembro@osiptel.test").status_code == 303
        assert member_client.get(f"/equipos/{team_id}/buscar?q=104").status_code == 200
        assert (
            member_client.get(f"/equipos/{team_id}/procesos/nuevo").status_code == 403
        )

    with _client(app) as leader_client:
        assert _login(leader_client, "lider@osiptel.test").status_code == 303
        excluded_job = _submit_job(
            leader_client, team_id, credential_id, "no-es-documento"
        )
        detail = leader_client.get(f"/equipos/{team_id}/procesos/{excluded_job}")
        assert detail.status_code == 200
        assert "sin registros válidos" in detail.text
        assert "Tarea" in detail.text
        stream = leader_client.get(
            f"/equipos/{team_id}/procesos/{excluded_job}/progreso",
            headers={"Last-Event-ID": "0"},
        )
        assert stream.status_code == 200
        assert "event: progreso" in stream.text and "Completado" in stream.text
        # A terminal job says so, because `sse-close="fin"` is what stops the
        # browser reconnecting to it for as long as the tab stays open.
        assert stream.text.endswith("event: fin\ndata: \n\n")
        reconnect = leader_client.get(
            f"/equipos/{team_id}/procesos/{excluded_job}/progreso",
            headers={"Last-Event-ID": "1"},
        )
        assert reconnect.status_code == 200
        assert reconnect.text == "event: fin\ndata: \n\n"

        active_job = _submit_job(leader_client, team_id, credential_id, "10412345678")
        assert await repository.cancel(active_job, team_id) is not None
        cancelled = leader_client.get(f"/equipos/{team_id}/procesos/{active_job}")
        assert "Cancelado" in cancelled.text

    with _client(app) as outsider_client:
        assert _login(outsider_client, "otro@osiptel.test").status_code == 303
        assert (
            outsider_client.get(
                f"/equipos/{team_id}/procesos/{excluded_job}"
            ).status_code
            == 403
        )
        assert (
            outsider_client.get(
                f"/equipos/{team_id}/procesos/{excluded_job}/progreso"
            ).status_code
            == 403
        )

    with _client(app) as anonymous_client:
        assert (
            anonymous_client.get(
                f"/equipos/{team_id}/procesos/{excluded_job}/progreso"
            ).status_code
            == 401
        )

    assert leader_id != member_id


async def test_csv_upload_uses_the_file_name_and_first_column(
    pool: asyncpg.Pool, repository: PostgresPortalRepository, app: FastAPI
) -> None:
    people = await _experience(pool, repository)
    team_id, credential_id = people.team_id, people.credential_id
    with _client(app) as client:
        assert _login(client, "lider@osiptel.test").status_code == 303
        csv_job = _submit_csv(client, team_id, credential_id)

    stored_job = await repository.job(csv_job, team_id)
    assert stored_job is not None
    assert stored_job.filename == "barranca.csv"
    assert [item.document for item in stored_job.items] == [
        "10412345678",
        "10412345679",
    ]


async def test_htmx_search_notifications_partial_results_and_pagination(
    pool: asyncpg.Pool, repository: PostgresPortalRepository, app: FastAPI
) -> None:
    people = await _experience(pool, repository)
    team_id, credential_id = people.team_id, people.credential_id
    with _client(app) as client:
        assert _login(client, "lider@osiptel.test").status_code == 303
        job_id = _submit_job(client, team_id, credential_id, "10412345678")
        claimed = await repository.claim("trabajador", ("osiptel",))
        assert claimed is not None and claimed.job_id == job_id
        assert await repository.publish(
            claimed.item_id,
            "trabajador",
            claimed.lease_fence,
            await object_reference(pool, team_id, "salida/uno.json"),
        )

        search = client.get(
            f"/equipos/{team_id}/buscar?q=104", headers={"HX-Request": "true"}
        )
        assert search.status_code == 200
        assert "10412345678" in search.text and "registros.csv" in search.text
        partial = client.get(
            f"/equipos/{team_id}/buscar?q=999", headers={"HX-Request": "true"}
        )
        assert "No hay resultados" in partial.text
        for number in range(20):
            _submit_job(client, team_id, credential_id, f"inválido-{number}")
        first_page = client.get(
            f"/equipos/{team_id}?page=1", headers={"HX-Request": "true"}
        )
        jobs = client.get(f"/equipos/{team_id}?page=2", headers={"HX-Request": "true"})
        assert "Siguiente" in first_page.text and "Anterior" in jobs.text
        notifications = client.get("/notificaciones", headers={"HX-Request": "true"})
        assert notifications.status_code == 200
        assert "Tarea completada" in notifications.text


async def test_one_url_serves_both_the_page_and_the_fragment_htmx_swaps_into_it(
    pool: asyncpg.Pool, repository: PostgresPortalRepository, app: FastAPI
) -> None:
    """A pushed htmx URL must reload as a whole page, not as a bare fragment."""
    people = await _experience(pool, repository)
    team_id, credential_id = people.team_id, people.credential_id
    with _client(app) as client:
        assert _login(client, "lider@osiptel.test").status_code == 303
        _submit_job(client, team_id, credential_id, "10412345678")

        for url in (f"/equipos/{team_id}", f"/equipos/{team_id}/buscar?q=104"):
            page = client.get(url)
            fragment = client.get(url, headers={"HX-Request": "true"})

            assert page.status_code == fragment.status_code == 200
            assert "<!DOCTYPE html>" in page.text
            assert "<!DOCTYPE html>" not in fragment.text
            # Without Vary a shared cache could replay the fragment to a browser.
            assert page.headers["vary"] == fragment.headers["vary"] == "HX-Request"
