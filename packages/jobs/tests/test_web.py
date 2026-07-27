from __future__ import annotations

import uuid

from pathlib import Path

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
