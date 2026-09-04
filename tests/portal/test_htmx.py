from __future__ import annotations

from typing import TYPE_CHECKING

from portal.security import hash_password

from tests.portal.conftest import (
    PASSWORD,
    build_experience,
    login,
    publish_claimed,
    seed_team,
    submit_job,
    sync_client,
)


if TYPE_CHECKING:
    from uuid import UUID

    import asyncpg

    from litestar import Litestar
    from litestar.testing import TestClient
    from portal.repository.jobs import PostgresJobRepository
    from portal.repository.teams import PostgresTeamRepository

HTMX = {"HX-Request": "true"}


async def _publish_result(
    pool: asyncpg.Pool,
    job_repository: PostgresJobRepository,
    client: TestClient,
    team_id: UUID,
    credential_id: UUID,
) -> UUID:
    job_id = submit_job(client, team_id, credential_id, "10412345678")

    claimed = await job_repository.claim("trabajador", ("osiptel",))
    assert claimed is not None
    assert claimed.job_id == job_id
    assert await publish_claimed(pool, job_repository, claimed)

    return job_id


async def test_htmx_search_finds_a_published_result(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: Litestar,
) -> None:
    experience = await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        await _publish_result(
            pool,
            job_repository,
            client,
            experience.team_id,
            experience.credential_id,
        )

        search = client.get(
            f"/teams/{experience.team_id}/search?q=104",
            headers=HTMX,
        )

    assert search.status_code == 200
    assert "10412345678" in search.text
    assert "osiptel" in search.text
    assert f"/teams/{experience.team_id}/entries/" in search.text


async def test_search_result_links_to_an_entry_detail_page_that_renders(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: Litestar,
) -> None:
    experience = await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        await _publish_result(
            pool,
            job_repository,
            client,
            experience.team_id,
            experience.credential_id,
        )

        entry_id = await pool.fetchval(
            "SELECT id FROM portal_entries WHERE document = '10412345678'"
        )
        detail = client.get(f"/teams/{experience.team_id}/entries/{entry_id}")

    assert detail.status_code == 200
    assert "10412345678" in detail.text
    assert "osiptel" in detail.text


async def test_a_team_cannot_reach_another_teams_entry_by_guessing_its_id(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: Litestar,
) -> None:
    """A team cannot read an entry confirmed only by another team."""
    owner = await build_experience(pool, team_repository)
    outsider_team = await seed_team(pool, password_hash=hash_password(PASSWORD))

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        await _publish_result(
            pool,
            job_repository,
            client,
            owner.team_id,
            owner.credential_id,
        )

        entry_id = await pool.fetchval(
            "SELECT id FROM portal_entries WHERE document = '10412345678'"
        )

    with sync_client(app) as outsider_client:
        assert (
            login(
                outsider_client,
                f"{outsider_team.actor_id.hex}@example.test",
            ).status_code
            == 303
        )
        response = outsider_client.get(
            f"/teams/{outsider_team.team_id}/entries/{entry_id}"
        )

    assert response.status_code == 404


async def test_htmx_search_shows_an_empty_state_for_no_matches(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: Litestar,
) -> None:
    experience = await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        await _publish_result(
            pool,
            job_repository,
            client,
            experience.team_id,
            experience.credential_id,
        )

        search = client.get(
            f"/teams/{experience.team_id}/search?q=999",
            headers=HTMX,
        )

    assert search.status_code == 200
    assert 'class="empty-state"' in search.text
    assert "Sin resultados" in search.text


async def test_htmx_job_list_pagination_links_to_the_next_and_previous_pages(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    experience = await build_experience(pool, team_repository)
    team_id = experience.team_id
    credential_id = experience.credential_id

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303

        for number in range(21):
            submit_job(client, team_id, credential_id, f"inválido-{number}")

        first_page = client.get(f"/teams/{team_id}?page=1", headers=HTMX)
        second_page = client.get(f"/teams/{team_id}?page=2", headers=HTMX)

    assert f'hx-get="/teams/{team_id}?page=2"' in first_page.text
    assert f'hx-get="/teams/{team_id}?page=1"' in second_page.text


async def test_notifications_lists_a_completed_job(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    experience = await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        submit_job(
            client,
            experience.team_id,
            experience.credential_id,
            "no-es-documento",
        )

        notifications = client.get("/notifications", headers=HTMX)

    assert notifications.status_code == 200
    assert "Tarea completada" in notifications.text


async def test_htmx_urls_serve_pages_and_fragments(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    experience = await build_experience(pool, team_repository)
    team_id = experience.team_id
    credential_id = experience.credential_id

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        submit_job(client, team_id, credential_id, "10412345678")

        for url in (
            f"/teams/{team_id}",
            f"/teams/{team_id}/search?q=104",
        ):
            page = client.get(url)
            fragment = client.get(url, headers=HTMX)

            assert page.status_code == 200
            assert fragment.status_code == 200
            assert "<!DOCTYPE html>" in page.text
            assert "<!DOCTYPE html>" not in fragment.text

            # A shared cache must distinguish full pages from HTMX fragments.
            assert page.headers["vary"] == "HX-Request"
            assert fragment.headers["vary"] == "HX-Request"
