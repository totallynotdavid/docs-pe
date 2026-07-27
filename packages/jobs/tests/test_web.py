from __future__ import annotations

import uuid

from pathlib import Path

import pytest

from fastapi.testclient import TestClient
from jobs.settings import Settings
from jobs.web import create_app


def test_anonymous_ui_redirects_to_login_and_api_stays_json(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            database_path=tmp_path / "jobs.sqlite3",
            object_root=tmp_path / "objects",
            session_secret=uuid.uuid4().hex,
        )
    )
    with TestClient(app) as client:
        ui_response = client.get("/", follow_redirects=False)
        api_response = client.get("/api/me")

    assert ui_response.status_code == 303
    assert ui_response.headers["location"] == "/login"
    assert api_response.status_code == 403
    assert api_response.json() == {"detail": "authentication required"}


def test_login_bootstrap_and_source_contract(tmp_path: Path) -> None:
    session_secret = uuid.uuid4().hex
    bootstrap_password = uuid.uuid4().hex
    app = create_app(
        Settings(
            database_path=tmp_path / "jobs.sqlite3",
            object_root=tmp_path / "objects",
            session_secret=session_secret,
            bootstrap_admin_email="admin@example.test",
            bootstrap_admin_password=bootstrap_password,
        )
    )
    with TestClient(app) as client:
        assert client.get("/api/sources").json()["sources"] == [
            {"name": "osiptel", "columns": ["modalidad", "numero", "operador"]},
            {"name": "sunat", "columns": ["tipo_doc", "num_doc", "nombre"]},
            {
                "name": "sunat_reps",
                "columns": [
                    "razon_social",
                    "doc_type",
                    "num_doc",
                    "nombre",
                    "cargo",
                    "fecha_desde",
                ],
            },
        ]
        response = client.post(
            "/api/auth/login",
            json={"email": "admin@example.test", "password": bootstrap_password},
        )
        assert response.status_code == 200
        assert response.json()["user"]["is_site_admin"] is True
        assert client.cookies.get("jobs_csrf")


def test_cookie_authenticated_mutation_requires_csrf(tmp_path: Path) -> None:
    password = uuid.uuid4().hex
    app = create_app(
        Settings(
            database_path=tmp_path / "jobs.sqlite3",
            object_root=tmp_path / "objects",
            session_secret=uuid.uuid4().hex,
            bootstrap_admin_email="admin@example.test",
            bootstrap_admin_password=password,
        )
    )
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/auth/login",
                json={"email": "admin@example.test", "password": password},
            ).status_code
            == 200
        )
        rejected = client.post("/api/admin/teams", json={"name": "Blocked"})
        assert rejected.status_code == 403
        rejected_form = client.post("/ui/admin/team", data={"name": "Blocked form"})
        assert rejected_form.status_code == 403

        csrf = client.cookies.get("jobs_csrf")
        accepted = client.post(
            "/api/admin/teams",
            json={"name": "Allowed"},
            headers={"X-CSRF-Token": str(csrf)},
        )
        assert accepted.status_code == 200
        accepted_form = client.post(
            "/ui/admin/team",
            data={"name": "Allowed form", "csrf_token": str(csrf)},
            follow_redirects=False,
        )
        assert accepted_form.status_code == 303


def test_secure_cookie_is_required_in_production_and_emitted_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("JOBS_ENV", "production")
    monkeypatch.setenv("JOBS_SESSION_SECRET", uuid.uuid4().hex)
    monkeypatch.setenv("JOBS_COOKIE_SECURE", "false")
    with pytest.raises(RuntimeError, match="JOBS_COOKIE_SECURE"):
        Settings.from_env()

    password = uuid.uuid4().hex
    app = create_app(
        Settings(
            database_path=tmp_path / "jobs.sqlite3",
            object_root=tmp_path / "objects",
            session_secret=uuid.uuid4().hex,
            bootstrap_admin_email="admin@example.test",
            bootstrap_admin_password=password,
            environment="production",
            cookie_secure=True,
        )
    )
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            json={"email": "admin@example.test", "password": password},
        )
        assert response.status_code == 200
        assert "Secure" in response.headers["set-cookie"]


def test_workspace_ui_uses_the_internal_tool_shell(tmp_path: Path) -> None:
    session_secret = uuid.uuid4().hex
    bootstrap_password = uuid.uuid4().hex
    app = create_app(
        Settings(
            database_path=tmp_path / "jobs.sqlite3",
            object_root=tmp_path / "objects",
            session_secret=session_secret,
            bootstrap_admin_email="admin@example.test",
            bootstrap_admin_password=bootstrap_password,
        )
    )
    with TestClient(app) as client:
        login = client.post(
            "/api/auth/login",
            json={"email": "admin@example.test", "password": bootstrap_password},
        )
        assert login.status_code == 200

        page = client.get("/")

    assert page.status_code == 200
    assert "app-shell" in page.text
    assert "Panel de trabajos" in page.text
    assert "lang='es'" in page.text
    assert "settings-panel" in page.text
    assert bootstrap_password not in page.text


def test_ui_routes_render_spanish_workspace_views(tmp_path: Path) -> None:
    password = uuid.uuid4().hex
    app = create_app(
        Settings(
            database_path=tmp_path / "jobs.sqlite3",
            object_root=tmp_path / "objects",
            session_secret=uuid.uuid4().hex,
            bootstrap_admin_email="admin@example.test",
            bootstrap_admin_password=password,
        )
    )
    with TestClient(app) as client:
        assert "Iniciar sesión" in client.get("/login").text
        invalid = client.post(
            "/login",
            data={"email": "admin@example.test", "password": "bad"},
            follow_redirects=False,
        )
        assert invalid.status_code == 303
        assert "error=invalid" in invalid.headers["location"]
        client.post(
            "/api/auth/login",
            json={"email": "admin@example.test", "password": password},
        )
        pages = {
            "/": "Panel de trabajos",
            "/search": "Buscar resultados",
            "/notifications": "Notificaciones",
            "/admin": "Administración",
        }
        for path, copy in pages.items():
            response = client.get(path)
            assert response.status_code == 200
            assert copy in response.text
            assert "lang='es'" in response.text
        assert "Sign in" not in client.get("/login").text


def test_ui_routes_keep_anonymous_boundary(tmp_path: Path) -> None:
    app = create_app(
        Settings(
            database_path=tmp_path / "jobs.sqlite3",
            object_root=tmp_path / "objects",
            session_secret=uuid.uuid4().hex,
        )
    )
    with TestClient(app) as client:
        for path in ("/search", "/notifications", "/admin", "/jobs/job-unknown"):
            response = client.get(path, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"] == "/login"
