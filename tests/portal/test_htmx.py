from __future__ import annotations

from typing import TYPE_CHECKING

from tests.portal.conftest import (
    build_experience,
    login,
    object_reference,
    submit_job,
    sync_client,
)


if TYPE_CHECKING:
    import asyncpg

    from fastapi import FastAPI
    from portal.repository.jobs import PostgresJobRepository
    from portal.repository.teams import PostgresTeamRepository

HTMX = {"HX-Request": "true"}


async def test_htmx_search_notifications_partial_results_and_pagination(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: FastAPI,
) -> None:
    people = await build_experience(pool, team_repository)
    team_id = people.team_id
    credential_id = people.credential_id

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303

        job_id = submit_job(client, team_id, credential_id, "10412345678")

        claimed = await job_repository.claim("trabajador", ("osiptel",))
        assert claimed is not None
        assert claimed.job_id == job_id

        published = await job_repository.publish(
            claimed.item_id,
            "trabajador",
            claimed.lease_fence,
            await object_reference(pool, team_id, "salida/uno.json"),
        )
        assert published

        search = client.get(
            f"/equipos/{team_id}/buscar?q=104",
            headers=HTMX,
        )
        assert search.status_code == 200
        assert "10412345678" in search.text
        assert "registros.csv" in search.text

        partial = client.get(
            f"/equipos/{team_id}/buscar?q=999",
            headers=HTMX,
        )
        assert "No hay resultados" in partial.text

        for number in range(20):
            submit_job(client, team_id, credential_id, f"inválido-{number}")

        first_page = client.get(
            f"/equipos/{team_id}?page=1",
            headers=HTMX,
        )
        second_page = client.get(
            f"/equipos/{team_id}?page=2",
            headers=HTMX,
        )

        assert "Siguiente" in first_page.text
        assert "Anterior" in second_page.text

        notifications = client.get("/notificaciones", headers=HTMX)
        assert notifications.status_code == 200
        assert "Tarea completada" in notifications.text


async def test_htmx_urls_serve_pages_and_fragments(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: FastAPI,
) -> None:
    people = await build_experience(pool, team_repository)
    team_id = people.team_id
    credential_id = people.credential_id

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        submit_job(client, team_id, credential_id, "10412345678")

        for url in (
            f"/equipos/{team_id}",
            f"/equipos/{team_id}/buscar?q=104",
        ):
            page = client.get(url)
            fragment = client.get(url, headers=HTMX)

            assert page.status_code == 200
            assert fragment.status_code == 200

            assert "<!DOCTYPE html>" in page.text
            assert "<!DOCTYPE html>" not in fragment.text

            # Without Vary, a shared cache could replay the fragment to a browser.
            assert page.headers["vary"] == "HX-Request"
            assert fragment.headers["vary"] == "HX-Request"
