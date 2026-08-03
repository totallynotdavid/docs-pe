"""The htmx contract every page shares: search/pagination/notification
fragments, and the one URL that must answer both a full page and a swap."""

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
    from portal.repository.postgres import PostgresPortalRepository


async def test_htmx_search_notifications_partial_results_and_pagination(
    pool: asyncpg.Pool, repository: PostgresPortalRepository, app: FastAPI
) -> None:
    people = await build_experience(pool, repository)
    team_id, credential_id = people.team_id, people.credential_id
    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        job_id = submit_job(client, team_id, credential_id, "10412345678")
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
            submit_job(client, team_id, credential_id, f"inválido-{number}")
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
    people = await build_experience(pool, repository)
    team_id, credential_id = people.team_id, people.credential_id
    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        submit_job(client, team_id, credential_id, "10412345678")

        for url in (f"/equipos/{team_id}", f"/equipos/{team_id}/buscar?q=104"):
            page = client.get(url)
            fragment = client.get(url, headers={"HX-Request": "true"})

            assert page.status_code == fragment.status_code == 200
            assert "<!DOCTYPE html>" in page.text
            assert "<!DOCTYPE html>" not in fragment.text
            # Without Vary a shared cache could replay the fragment to a browser.
            assert page.headers["vary"] == fragment.headers["vary"] == "HX-Request"
