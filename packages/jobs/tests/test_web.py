from __future__ import annotations

import uuid

from pathlib import Path

import pytest

from fastapi.testclient import TestClient
from jobs.settings import Settings
from jobs.web import create_app


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
    assert "Jobs workspace" in page.text
    assert "settings-panel" in page.text
    assert bootstrap_password not in page.text
