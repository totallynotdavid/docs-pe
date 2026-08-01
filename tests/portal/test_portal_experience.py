from __future__ import annotations

import re

from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from portal.domain.models import CredentialVersion, TeamRole
from portal.repository.memory import InMemoryPortalRepository
from portal.settings import PortalSettings
from portal.web.app import create_app
from portal.web.security import hash_password


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


def _experience() -> tuple[InMemoryPortalRepository, UUID, UUID, UUID, UUID, UUID]:
    repository = InMemoryPortalRepository()
    admin = repository.add_user(
        "admin@osiptel.test", hash_password(PASSWORD), is_site_admin=True
    )
    leader = repository.add_user("lider@osiptel.test", hash_password(PASSWORD))
    member = repository.add_user("miembro@osiptel.test", hash_password(PASSWORD))
    outsider = repository.add_user("otro@osiptel.test", hash_password(PASSWORD))
    team_id = uuid4()
    other_team_id = uuid4()
    repository.grant(leader.id, team_id, TeamRole.TEAM_LEADER)
    repository.grant(member.id, team_id, TeamRole.TEAM_MEMBER)
    repository.grant(outsider.id, other_team_id, TeamRole.TEAM_LEADER)
    credential_id = uuid4()
    repository.add_credential(
        CredentialVersion(credential_id, team_id, "Proxy Lima", 1)
    )
    return repository, admin.id, leader.id, member.id, team_id, credential_id


def _client(repository: InMemoryPortalRepository) -> TestClient:
    return TestClient(
        create_app(
            PortalSettings("", public_origin=ORIGIN),
            repository=repository,
        )
    )


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


def test_login_csrf_cookie_rotation_and_generic_failure() -> None:
    repository, _, _, _, _, _ = _experience()
    with _client(repository) as client:
        page = client.get("/login")
        assert 'class="barra-superior"' not in page.text
        assert 'class="marca-auth"' in page.text
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


def test_new_job_makes_source_outcomes_visible_without_exposing_setup_details() -> None:
    repository, _, _, _, team_id, _ = _experience()
    with _client(repository) as client:
        assert _login(client, "lider@osiptel.test").status_code == 303
        page = client.get(f"/equipos/{team_id}/procesos/nuevo")

    assert page.status_code == 200
    assert "DNI y nombre" in page.text
    assert "Para RUC que empiezan en 10" in page.text
    assert "Recibirás: DNI y nombre de la persona." in page.text
    assert "Así se verá el resultado" in page.text
    assert "DNI" in page.text and "Nombre" in page.text
    assert 'value="sunat" checked' in page.text
    assert "Nombre de la consulta" not in page.text
    assert "versión 1" not in page.text


async def test_roles_cross_team_isolation_submission_and_terminal_rendering() -> None:
    repository, _, leader_id, member_id, team_id, credential_id = _experience()
    with _client(repository) as member_client:
        assert _login(member_client, "miembro@osiptel.test").status_code == 303
        assert member_client.get(f"/equipos/{team_id}/buscar?q=104").status_code == 200
        assert (
            member_client.get(f"/equipos/{team_id}/procesos/nuevo").status_code == 403
        )

    with _client(repository) as leader_client:
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
        assert await repository.cancel(active_job, team_id)
        cancelled = leader_client.get(f"/equipos/{team_id}/procesos/{active_job}")
        assert "Cancelado" in cancelled.text

    with _client(repository) as outsider_client:
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

    with _client(repository) as anonymous_client:
        assert (
            anonymous_client.get(
                f"/equipos/{team_id}/procesos/{excluded_job}/progreso"
            ).status_code
            == 401
        )

    assert leader_id != member_id


async def test_csv_upload_uses_the_file_name_and_first_column() -> None:
    repository, _, _, _, team_id, credential_id = _experience()
    with _client(repository) as client:
        assert _login(client, "lider@osiptel.test").status_code == 303
        csv_job = _submit_csv(client, team_id, credential_id)

    stored_job = await repository.job(csv_job, team_id)
    assert stored_job is not None
    assert stored_job.filename == "barranca.csv"
    assert [item.document for item in stored_job.items] == [
        "10412345678",
        "10412345679",
    ]


async def test_htmx_search_notifications_partial_results_and_pagination() -> None:
    repository, _, _, _, team_id, credential_id = _experience()
    with _client(repository) as client:
        assert _login(client, "lider@osiptel.test").status_code == 303
        job_id = _submit_job(client, team_id, credential_id, "10412345678")
        job = repository.jobs[job_id]
        assert await repository.record_published_result(
            job_id, job.items[0].id, job.lease_fence, uuid4()
        )
        await repository.complete(job_id)

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


def test_one_url_serves_both_the_page_and_the_fragment_htmx_swaps_into_it() -> None:
    """A pushed htmx URL must reload as a whole page, not as a bare fragment."""
    repository, _, _, _, team_id, credential_id = _experience()
    with _client(repository) as client:
        assert _login(client, "lider@osiptel.test").status_code == 303
        _submit_job(client, team_id, credential_id, "10412345678")

        for url in (f"/equipos/{team_id}", f"/equipos/{team_id}/buscar?q=104"):
            page = client.get(url)
            fragment = client.get(url, headers={"HX-Request": "true"})

            assert page.status_code == fragment.status_code == 200
            assert "<!doctype html>" in page.text
            assert "<!doctype html>" not in fragment.text
            # Without Vary a shared cache could replay the fragment to a browser.
            assert page.headers["vary"] == fragment.headers["vary"] == "HX-Request"
