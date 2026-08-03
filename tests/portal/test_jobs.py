"""The job lifecycle `routes/jobs.py` drives: submission, RBAC, SSE progress,
cancellation, and CSV parsing, exercised end to end against real PostgreSQL."""

from __future__ import annotations

import re

from typing import TYPE_CHECKING

from tests.portal.conftest import (
    build_experience,
    login,
    submit_csv,
    submit_job,
    sync_client,
)


if TYPE_CHECKING:
    import asyncpg

    from fastapi import FastAPI
    from portal.repository.jobs import PostgresJobRepository
    from portal.repository.teams import PostgresTeamRepository


async def test_new_job_makes_source_outcomes_visible_without_exposing_setup_details(
    pool: asyncpg.Pool, team_repository: PostgresTeamRepository, app: FastAPI
) -> None:
    team_id = (await build_experience(pool, team_repository)).team_id
    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
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
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: FastAPI,
) -> None:
    people = await build_experience(pool, team_repository)
    leader_id, member_id = people.leader_id, people.member_id
    team_id, credential_id = people.team_id, people.credential_id
    with sync_client(app) as member_client:
        assert login(member_client, "miembro@osiptel.test").status_code == 303
        assert member_client.get(f"/equipos/{team_id}/buscar?q=104").status_code == 200
        assert (
            member_client.get(f"/equipos/{team_id}/procesos/nuevo").status_code == 403
        )

    with sync_client(app) as leader_client:
        assert login(leader_client, "lider@osiptel.test").status_code == 303
        excluded_job = submit_job(
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

        active_job = submit_job(leader_client, team_id, credential_id, "10412345678")
        assert await job_repository.cancel(active_job, team_id) is not None
        cancelled = leader_client.get(f"/equipos/{team_id}/procesos/{active_job}")
        assert "Cancelado" in cancelled.text

    with sync_client(app) as outsider_client:
        assert login(outsider_client, "otro@osiptel.test").status_code == 303
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

    with sync_client(app) as anonymous_client:
        assert (
            anonymous_client.get(
                f"/equipos/{team_id}/procesos/{excluded_job}/progreso"
            ).status_code
            == 401
        )

    assert leader_id != member_id


async def test_csv_upload_uses_the_file_name_and_first_column(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: FastAPI,
) -> None:
    people = await build_experience(pool, team_repository)
    team_id, credential_id = people.team_id, people.credential_id
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
