from __future__ import annotations

import re

from typing import TYPE_CHECKING
from uuid import uuid4

from tests.portal.conftest import (
    build_experience,
    login,
    submit_csv,
    submit_job,
    sync_client,
)


if TYPE_CHECKING:
    import asyncpg

    from litestar import Litestar
    from portal.repository.jobs import PostgresJobRepository
    from portal.repository.teams import PostgresTeamRepository


async def test_new_job_makes_source_outcomes_visible_without_exposing_setup_details(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    team_id = (await build_experience(pool, team_repository)).team_id

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        page = client.get(f"/teams/{team_id}/jobs/new")

    assert page.status_code == 200
    assert "DNI y nombre" in page.text
    assert "Para RUC que empiezan en 10" in page.text
    assert "Recibirás: DNI y nombre de la persona." in page.text
    assert "Así se verá el resultado" in page.text
    assert "DNI" in page.text
    assert "Nombre" in page.text
    assert re.search(r'value="sunat"\s+checked', page.text)
    assert "Nombre de la consulta" not in page.text
    assert "versión 1" not in page.text


async def test_team_member_can_search_but_not_create_jobs(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    team_id = (await build_experience(pool, team_repository)).team_id

    with sync_client(app) as client:
        assert login(client, "miembro@osiptel.test").status_code == 303

        search = client.get(f"/teams/{team_id}/search?q=104")
        new_job = client.get(f"/teams/{team_id}/jobs/new")

    assert search.status_code == 200
    assert new_job.status_code == 403


async def test_job_detail_shows_why_input_was_excluded(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    people = await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        job_id = submit_job(
            client, people.team_id, people.credential_id, "no-es-documento"
        )

        detail = client.get(f"/teams/{people.team_id}/jobs/{job_id}")

    assert detail.status_code == 200
    assert "sin registros válidos" in detail.text
    assert "Tarea" in detail.text


async def test_a_finished_job_streams_progress_then_a_done_event(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    people = await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        job_id = submit_job(
            client, people.team_id, people.credential_id, "no-es-documento"
        )

        stream = client.get(
            f"/teams/{people.team_id}/jobs/{job_id}/progress",
            headers={"Last-Event-ID": "0"},
        )

    assert stream.status_code == 200
    assert "event: progress" in stream.text
    assert "Completado" in stream.text

    # A "done" event stops the browser's EventSource from reconnecting.
    assert stream.text.endswith("event: done\r\ndata: \r\n\r\n")


async def test_reconnecting_after_the_terminal_state_gets_only_the_done_event(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    people = await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        job_id = submit_job(
            client, people.team_id, people.credential_id, "no-es-documento"
        )

        reconnect = client.get(
            f"/teams/{people.team_id}/jobs/{job_id}/progress",
            headers={"Last-Event-ID": "1"},
        )

    assert reconnect.status_code == 200
    assert reconnect.text == "event: done\r\ndata: \r\n\r\n"


async def test_a_cancelled_jobs_detail_shows_as_cancelled(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: Litestar,
) -> None:
    people = await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        job_id = submit_job(client, people.team_id, people.credential_id, "10412345678")

        assert await job_repository.cancel(job_id, people.team_id) is not None

        cancelled = client.get(f"/teams/{people.team_id}/jobs/{job_id}")

    assert cancelled.status_code == 200
    assert "Cancelado" in cancelled.text


async def test_job_detail_and_progress_are_forbidden_to_outsiders(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    people = await build_experience(pool, team_repository)

    with sync_client(app) as leader_client:
        assert login(leader_client, "lider@osiptel.test").status_code == 303
        job_id = submit_job(
            leader_client, people.team_id, people.credential_id, "no-es-documento"
        )

    with sync_client(app) as outsider_client:
        assert login(outsider_client, "otro@osiptel.test").status_code == 303

        detail = outsider_client.get(f"/teams/{people.team_id}/jobs/{job_id}")
        stream = outsider_client.get(f"/teams/{people.team_id}/jobs/{job_id}/progress")

    assert detail.status_code == 403
    assert stream.status_code == 403


def test_job_progress_requires_authentication(app: Litestar) -> None:
    with sync_client(app) as anonymous_client:
        stream = anonymous_client.get(f"/teams/{uuid4()}/jobs/{uuid4()}/progress")

    assert stream.status_code == 401


async def test_csv_upload_uses_the_file_name_and_first_column(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: Litestar,
) -> None:
    people = await build_experience(pool, team_repository)
    team_id = people.team_id
    credential_id = people.credential_id

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        csv_job = submit_csv(client, team_id, credential_id)

    stored_job = await job_repository.job(csv_job, team_id)

    assert stored_job is not None
    assert stored_job.filename == "barranca.csv"
    assert [item.document for item in stored_job.items] == [
        "10412345678",
        "10412345679",
    ]
