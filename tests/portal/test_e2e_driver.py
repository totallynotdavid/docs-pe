"""The end-to-end hook's portal steps, run against the real web app, worker
API, database and object store."""

from __future__ import annotations

import base64
import json

from typing import TYPE_CHECKING
from uuid import UUID

import pyotp
import pytest

from portal.security import hash_password

from ops.e2e_portal import E2EError, check_job, enrol_totp, sign_in, submit_job
from tests.portal.conftest import (
    PASSWORD,
    WORKER_ID,
    build_experience,
    enroll_worker,
    publish_claimed,
    sync_client,
)


if TYPE_CHECKING:
    import asyncpg

    from litestar import Litestar
    from litestar.testing import AsyncTestClient
    from portal.application.provisioning import ProvisioningService
    from portal.repository.jobs import PostgresJobRepository
    from portal.repository.teams import PostgresTeamRepository
    from portal.settings import PortalSettings


async def test_the_pending_administrator_enrols_totp_and_signs_in_with_it(
    provisioning: ProvisioningService, app: Litestar
) -> None:
    await provisioning.ensure_site_admin("admin@osiptel.test", hash_password(PASSWORD))

    with sync_client(app) as client:
        sign_in(client, "admin@osiptel.test", PASSWORD)
        secret = enrol_totp(client)

    with sync_client(app) as client, pytest.raises(E2EError, match="second factor"):
        sign_in(client, "admin@osiptel.test", PASSWORD)

    with sync_client(app) as client:
        sign_in(client, "admin@osiptel.test", PASSWORD, pyotp.TOTP(secret))


async def test_a_resubmitted_document_is_looked_up_again(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: Litestar,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        sign_in(client, "lider@osiptel.test", PASSWORD)
        first = submit_job(client, "osiptel", "10412345678")

        claimed = await job_repository.claim("trabajador", ("osiptel",))
        assert claimed is not None
        assert await publish_claimed(pool, job_repository, claimed)

        second = submit_job(client, "osiptel", "10412345678")

    assert second != first
    state = await pool.fetchval(
        "SELECT state FROM portal_job_items WHERE job_id = $1", UUID(second)
    )
    assert state == "pending"


@pytest.fixture
def portal_env(settings: PortalSettings, monkeypatch: pytest.MonkeyPatch) -> None:
    """The web container's environment, which check_job runs in."""
    for name, value in {
        "PORTAL_DATABASE_DSN": settings.database_dsn,
        "PORTAL_OBJECT_STORAGE_ENDPOINT": settings.object_storage_endpoint,
        "PORTAL_OBJECT_STORAGE_BUCKET": settings.object_storage_bucket,
        "PORTAL_OBJECT_STORAGE_ACCESS_KEY": settings.object_storage_access_key,
        "PORTAL_OBJECT_STORAGE_SECRET_KEY": settings.object_storage_secret_key,
        "PORTAL_OBJECT_STORAGE_REGION": settings.object_storage_region,
    }.items():
        monkeypatch.setenv(name, value)


async def published_job(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: Litestar,
    worker_client: AsyncTestClient,
) -> UUID:
    """A job whose one item the worker published through the worker API."""
    await build_experience(pool, team_repository)
    headers = await enroll_worker(pool)

    with sync_client(app) as client:
        sign_in(client, "lider@osiptel.test", PASSWORD)
        job_id = UUID(submit_job(client, "osiptel", "10412345678"))

    claimed = await job_repository.claim(WORKER_ID, ("osiptel",))
    assert claimed is not None

    response = await worker_client.post(
        "/publish",
        headers=headers,
        json={
            "item_id": str(claimed.item_id),
            "fence": claimed.lease_fence,
            "lane_index": 0,
            "source": "osiptel",
            "provider": "geonode",
            "healthy_contact": True,
            "document": claimed.document,
            "status": "not_found",
            "columns": [],
            "rows": [],
            "error_code": None,
            "content": base64.b64encode(json.dumps([]).encode()).decode(),
            "attempts": [{"fetch_attempt": 1, "outcome": "not_found", "elapsed_ms": 5}],
        },
    )
    assert response.json() == {"published": True}

    return job_id


@pytest.mark.usefixtures("portal_env")
async def test_check_job_accepts_a_result_the_worker_stored(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: Litestar,
    worker_client: AsyncTestClient,
) -> None:
    job_id = await published_job(
        pool, team_repository, job_repository, app, worker_client
    )

    await check_job(job_id, WORKER_ID, timeout_seconds=5)


@pytest.mark.usefixtures("portal_env")
async def test_check_job_refuses_a_result_from_another_worker(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    app: Litestar,
    worker_client: AsyncTestClient,
) -> None:
    job_id = await published_job(
        pool, team_repository, job_repository, app, worker_client
    )

    with pytest.raises(E2EError, match="looked up by"):
        await check_job(job_id, "otro", timeout_seconds=5)


@pytest.mark.usefixtures("portal_env")
async def test_check_job_refuses_a_job_that_never_finishes(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    await build_experience(pool, team_repository)

    with sync_client(app) as client:
        sign_in(client, "lider@osiptel.test", PASSWORD)
        job_id = UUID(submit_job(client, "osiptel", "10412345678"))

    with pytest.raises(E2EError, match="still running"):
        await check_job(job_id, WORKER_ID, timeout_seconds=0)
