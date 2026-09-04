from __future__ import annotations

import re

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from portal.domain.models import ItemState, JobState
from portal.web.uploads import MAX_REQUEST_BODY_BYTES

from tests.portal.conftest import (
    ORIGIN,
    build_experience,
    csrf_token,
    hidden_value,
    login,
    publish_claimed,
    session_csrf,
    submit_csv,
    submit_job,
    sync_client,
)


if TYPE_CHECKING:
    import asyncpg
    import httpx

    from litestar import Litestar
    from litestar.testing import TestClient
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


async def test_team_member_landing_on_team_page_is_sent_to_search(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    """A plain member has nothing to manage on this page: send them straight
    to the search they can actually use instead of an empty-state detour."""
    team_id = (await build_experience(pool, team_repository)).team_id

    with sync_client(app) as client:
        assert login(client, "miembro@osiptel.test").status_code == 303

        landing = client.get(f"/teams/{team_id}", follow_redirects=False)

    assert landing.status_code == 303
    assert landing.headers["location"] == f"/teams/{team_id}/search"


async def test_job_detail_shows_why_input_was_excluded(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    people = await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        job_id = submit_job(
            client,
            people.team_id,
            people.credential_id,
            "no-es-documento",
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
            client,
            people.team_id,
            people.credential_id,
            "no-es-documento",
        )

        stream = client.get(
            f"/teams/{people.team_id}/jobs/{job_id}/progress",
            headers={"Last-Event-ID": "0"},
        )

    assert stream.status_code == 200
    assert "event: progress" in stream.text
    assert "Completado" in stream.text
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
            client,
            people.team_id,
            people.credential_id,
            "no-es-documento",
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
        job_id = submit_job(
            client,
            people.team_id,
            people.credential_id,
            "10412345678",
        )

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
            leader_client,
            people.team_id,
            people.credential_id,
            "no-es-documento",
        )

    with sync_client(app) as outsider_client:
        assert login(outsider_client, "otro@osiptel.test").status_code == 303

        detail = outsider_client.get(
            f"/teams/{people.team_id}/jobs/{job_id}",
        )
        stream = outsider_client.get(
            f"/teams/{people.team_id}/jobs/{job_id}/progress",
        )

    assert detail.status_code == 403
    assert stream.status_code == 403


def test_job_progress_requires_authentication(app: Litestar) -> None:
    with sync_client(app) as anonymous_client:
        stream = anonymous_client.get(
            f"/teams/{uuid4()}/jobs/{uuid4()}/progress",
        )

    assert stream.status_code == 401


async def test_csv_upload_uses_the_file_name_and_first_column(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: Litestar,
) -> None:
    people = await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        job_id = submit_csv(
            client,
            people.team_id,
            people.credential_id,
        )

    stored_job = await job_repository.job(job_id, people.team_id)

    assert stored_job is not None
    assert stored_job.filename == "barranca.csv"
    assert [item.document for item in stored_job.items] == [
        "10412345678",
        "10412345679",
    ]


async def test_csv_upload_over_the_body_limit_gets_the_friendly_message(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    """Litestar's request_max_body_size rejects this before new_job_post runs,
    ahead of read_csv_upload's own checks: the app must still render its own
    CSV_TOO_LARGE message rather than Litestar's generic 413 body.
    """
    people = await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303

        response = client.post(
            f"/teams/{people.team_id}/jobs",
            data={
                "credential_version_id": str(people.credential_id),
                "sources": "osiptel",
                "csrf_token": session_csrf(client),
            },
            files={
                "input_file": (
                    "enorme.csv",
                    b"0" * (MAX_REQUEST_BODY_BYTES + 1),
                    "text/csv",
                )
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

    assert response.status_code == 413
    assert "el archivo CSV no puede superar los 15 MB" in response.text


def _upload_csv(
    client: TestClient,
    team_id: UUID,
    credential_id: UUID,
    documents: str,
) -> httpx.Response:
    """POST /jobs directly, without submit_job's 303-on-success assumption:
    a submission this team already has a fresh answer for renders the
    JobReview screen (200) instead of admitting immediately."""
    return client.post(
        f"/teams/{team_id}/jobs",
        data={
            "credential_version_id": str(credential_id),
            "sources": "osiptel",
            "csrf_token": session_csrf(client),
        },
        files={"input_file": ("consulta.csv", documents.encode(), "text/csv")},
        headers={"Origin": ORIGIN},
        follow_redirects=False,
    )


async def test_a_fully_reusable_submission_shows_the_review_screen(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: Litestar,
) -> None:
    people = await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303

        first_job_id = submit_job(
            client,
            people.team_id,
            people.credential_id,
            "10412345678",
        )
        claimed = await job_repository.claim("trabajador", ("osiptel",))
        assert claimed is not None
        assert claimed.job_id == first_job_id
        assert await publish_claimed(pool, job_repository, claimed)

        review = _upload_csv(
            client,
            people.team_id,
            people.credential_id,
            "10412345678",
        )

    assert review.status_code == 200
    assert "Revisar antes de consultar" in review.text
    assert "1 de 1" in review.text


async def test_confirming_a_single_source_review_reuses_without_refetching(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: Litestar,
) -> None:
    """Regression: Litestar's URL-encoded body binding decodes a single-value
    `sources` field as a scalar, not a list, so a structured Body(URL_ENCODED)
    form here 400s whenever a leader picks (or has cached) exactly one
    source. confirm_job_post reads request.form() raw to accept that input."""
    people = await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303

        first_job_id = submit_job(
            client,
            people.team_id,
            people.credential_id,
            "10412345678",
        )
        claimed = await job_repository.claim("trabajador", ("osiptel",))
        assert claimed is not None
        assert claimed.job_id == first_job_id
        assert await publish_claimed(pool, job_repository, claimed)

        review = _upload_csv(
            client,
            people.team_id,
            people.credential_id,
            "10412345678",
        )
        assert review.status_code == 200

        confirm = client.post(
            f"/teams/{people.team_id}/jobs/confirm",
            data={
                "input_object_id": hidden_value(review.text, "input_object_id"),
                "credential_version_id": hidden_value(
                    review.text, "credential_version_id"
                ),
                "filename": hidden_value(review.text, "filename"),
                "sources": "osiptel",
                "reuse": "reuse",
                "csrf_token": csrf_token(review.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

    assert confirm.status_code == 303
    second_job_id = UUID(confirm.headers["location"].rsplit("/", 1)[1])
    second_job = await job_repository.job(second_job_id, people.team_id)

    assert second_job is not None
    assert second_job.state is JobState.COMPLETED
    [item] = second_job.items
    assert item.state is ItemState.PUBLISHED
    assert item.entry_id is not None


async def test_confirming_with_rescan_forces_a_fresh_fetch(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: Litestar,
) -> None:
    people = await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303

        first_job_id = submit_job(
            client,
            people.team_id,
            people.credential_id,
            "10412345678",
        )
        claimed = await job_repository.claim("trabajador", ("osiptel",))
        assert claimed is not None
        assert claimed.job_id == first_job_id
        assert await publish_claimed(pool, job_repository, claimed)

        review = _upload_csv(
            client,
            people.team_id,
            people.credential_id,
            "10412345678",
        )
        assert review.status_code == 200

        confirm = client.post(
            f"/teams/{people.team_id}/jobs/confirm",
            data={
                "input_object_id": hidden_value(review.text, "input_object_id"),
                "credential_version_id": hidden_value(
                    review.text, "credential_version_id"
                ),
                "filename": hidden_value(review.text, "filename"),
                "sources": "osiptel",
                "reuse": "rescan",
                "csrf_token": csrf_token(review.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

    assert confirm.status_code == 303
    second_job_id = UUID(confirm.headers["location"].rsplit("/", 1)[1])
    second_job = await job_repository.job(second_job_id, people.team_id)

    assert second_job is not None
    assert second_job.state is JobState.RUNNING
    [item] = second_job.items
    assert item.state is ItemState.PENDING
    assert item.entry_id is None


async def test_leader_can_download_job_results_as_csv(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: Litestar,
) -> None:
    people = await build_experience(pool, team_repository)

    with sync_client(app) as client:
        assert login(client, "lider@osiptel.test").status_code == 303
        job_id = submit_job(
            client,
            people.team_id,
            people.credential_id,
            "10412345678",
        )
        claimed = await job_repository.claim("trabajador", ("osiptel",))
        assert claimed is not None
        assert await publish_claimed(
            pool,
            job_repository,
            claimed,
            columns=("Modalidad", "Número"),
            rows=(("Postpago", "98765"),),
        )

        download = client.get(f"/teams/{people.team_id}/jobs/{job_id}/download")

    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/csv")
    assert 'attachment; filename="' in download.headers["content-disposition"]
    lines = download.text.splitlines()
    assert lines[0] == "Documento,Fuente,Estado,Resultado,Modalidad,Número"
    assert lines[1] == "10412345678,osiptel,Publicado,Encontrado,Postpago,98765"


async def test_site_admin_can_download_job_results_without_membership(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    people = await build_experience(pool, team_repository)

    with sync_client(app) as leader_client:
        assert login(leader_client, "lider@osiptel.test").status_code == 303
        job_id = submit_job(
            leader_client,
            people.team_id,
            people.credential_id,
            "no-es-documento",
        )

    with sync_client(app) as admin_client:
        assert login(admin_client, "admin@osiptel.test").status_code == 303
        download = admin_client.get(
            f"/teams/{people.team_id}/jobs/{job_id}/download",
        )

    assert download.status_code == 200


async def test_team_member_cannot_download_job_results(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    people = await build_experience(pool, team_repository)

    with sync_client(app) as leader_client:
        assert login(leader_client, "lider@osiptel.test").status_code == 303
        job_id = submit_job(
            leader_client,
            people.team_id,
            people.credential_id,
            "no-es-documento",
        )

    with sync_client(app) as member_client:
        assert login(member_client, "miembro@osiptel.test").status_code == 303
        download = member_client.get(
            f"/teams/{people.team_id}/jobs/{job_id}/download",
        )

    assert download.status_code == 403
