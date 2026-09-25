from __future__ import annotations

import dataclasses
import logging
import socket

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import asyncpg
import httpx
import pytest

from litestar.testing import AsyncTestClient
from portal.domain.errors import PermissionDenied
from portal.repository.jobs import PostgresJobRepository
from portal.repository.workers import PostgresWorkerRegistry, worker_role_name
from portal.worker import agent
from portal.worker.api import create_worker_api


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from litestar import Litestar
    from portal.settings import PortalSettings


BOOTSTRAP_TOKEN = "bootstrap-secret"


@pytest.fixture(autouse=True)
async def _drop_minted_worker_roles(
    settings: PortalSettings,
) -> AsyncIterator[None]:
    """Drop the cluster-wide login role created by enrollment."""
    yield

    connection = await asyncpg.connect(settings.database_dsn)

    try:
        await connection.execute(f'DROP ROLE IF EXISTS "{worker_role_name("node-1")}"')
    finally:
        await connection.close()


@pytest.fixture
def bootstrapped_api(settings: PortalSettings) -> Litestar:
    return create_worker_api(
        dataclasses.replace(settings, worker_bootstrap_token=BOOTSTRAP_TOKEN)
    )


@pytest.fixture
async def bootstrapped_client(
    bootstrapped_api: Litestar,
) -> AsyncIterator[AsyncTestClient]:
    async with AsyncTestClient(
        app=bootstrapped_api,
        base_url="https://worker-api.tailnet.test",
    ) as http_client:
        yield http_client


async def test_self_enroll_issues_a_credential_the_registry_accepts(
    bootstrapped_client: AsyncTestClient,
    pool: asyncpg.Pool,
) -> None:
    response = await bootstrapped_client.post(
        "/enroll",
        json={"worker_id": "aws-1", "tailscale_hostname": "aws.tailnet.test"},
        headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"},
    )

    assert response.status_code == 200
    credential = response.json()["credential"]
    assert credential

    identity = await PostgresWorkerRegistry(pool).authorize("aws-1", credential)
    assert identity.worker_id == "aws-1"
    assert identity.tailscale_hostname == "aws.tailnet.test"


async def test_self_enroll_rejects_the_wrong_bootstrap_token(
    bootstrapped_client: AsyncTestClient,
) -> None:
    response = await bootstrapped_client.post(
        "/enroll",
        json={"worker_id": "aws-1", "tailscale_hostname": "aws.tailnet.test"},
        headers={"Authorization": "Bearer not-the-token"},
    )

    assert response.status_code == 403
    assert response.json()["reason"] == "worker_bootstrap_invalid"


async def test_self_enroll_is_refused_when_no_bootstrap_token_is_configured(
    worker_client: AsyncTestClient,
) -> None:
    """An empty configured bootstrap token never authorizes an empty bearer."""
    response = await worker_client.post(
        "/enroll",
        json={"worker_id": "aws-1", "tailscale_hostname": "aws.tailnet.test"},
        headers={"Authorization": "Bearer "},
    )

    assert response.status_code == 403


async def test_self_enroll_is_idempotent_by_worker_id(
    bootstrapped_client: AsyncTestClient,
    pool: asyncpg.Pool,
) -> None:
    """Re-enrollment replaces the previous credential for the worker id."""
    headers = {"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"}
    body = {"worker_id": "aws-1", "tailscale_hostname": "aws.tailnet.test"}

    first = await bootstrapped_client.post("/enroll", json=body, headers=headers)
    second = await bootstrapped_client.post("/enroll", json=body, headers=headers)

    first_credential = first.json()["credential"]
    second_credential = second.json()["credential"]
    assert first_credential != second_credential

    registry = PostgresWorkerRegistry(pool)

    with pytest.raises(PermissionDenied):
        await registry.authorize("aws-1", first_credential)

    identity = await registry.authorize("aws-1", second_credential)
    assert identity.worker_id == "aws-1"


async def test_self_enroll_rejects_an_invalid_worker_id(
    bootstrapped_client: AsyncTestClient,
) -> None:
    response = await bootstrapped_client.post(
        "/enroll",
        json={"worker_id": "not a valid id!", "tailscale_hostname": "aws.tailnet.test"},
        headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"},
    )

    assert response.status_code == 403


async def test_self_enroll_mints_a_working_direct_db_role(
    bootstrapped_client: AsyncTestClient,
    settings: PortalSettings,
) -> None:
    """Enrollment returns a DSN for a role limited to worker operations."""
    response = await bootstrapped_client.post(
        "/enroll",
        json={"worker_id": "node-1", "tailscale_hostname": "node-1.tailnet.test"},
        headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"},
    )

    assert response.status_code == 200
    dsn = response.json()["database_dsn"]
    role = worker_role_name("node-1")
    assert f"{role}:" in dsn
    assert dsn != settings.database_dsn

    worker_pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)

    try:
        claimed = await PostgresJobRepository(worker_pool).claim_many(
            "node-1", ("osiptel",), 1
        )
        assert claimed == ()

        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await worker_pool.fetchval(
                "SELECT config_ciphertext FROM "
                "portal_team_proxy_credential_versions LIMIT 1"
            )
    finally:
        await worker_pool.close()


async def test_enrolled_dsn_addresses_the_database_as_workers_reach_it(
    settings: PortalSettings,
) -> None:
    """The DSN names the worker-facing address, not the core's own DSN host."""
    api = create_worker_api(
        dataclasses.replace(
            settings,
            worker_bootstrap_token=BOOTSTRAP_TOKEN,
            worker_database_address="db.worker.test:6543",
        )
    )

    async with AsyncTestClient(
        app=api, base_url="https://worker-api.tailnet.test"
    ) as client:
        response = await client.post(
            "/enroll",
            json={"worker_id": "node-1", "tailscale_hostname": "node-1.tailnet.test"},
            headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"},
        )

    assert response.status_code == 200
    enrolled = urlsplit(response.json()["database_dsn"])
    admin = urlsplit(settings.database_dsn)
    assert enrolled.hostname == "db.worker.test"
    assert enrolled.port == 6543
    assert enrolled.username == worker_role_name("node-1")
    assert enrolled.path == admin.path


async def test_re_enrolling_re_keys_the_direct_db_role_idempotently(
    bootstrapped_client: AsyncTestClient,
) -> None:
    """Re-enrollment rotates the worker's Postgres login password."""
    headers = {"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"}
    body = {"worker_id": "node-1", "tailscale_hostname": "node-1.tailnet.test"}

    first = await bootstrapped_client.post("/enroll", json=body, headers=headers)
    second = await bootstrapped_client.post("/enroll", json=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["database_dsn"] != second.json()["database_dsn"]

    worker_pool = await asyncpg.create_pool(
        second.json()["database_dsn"], min_size=1, max_size=1
    )
    await worker_pool.close()


async def test_enrollment_retries_log_one_line_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    monkeypatch.setattr(agent, "ENROLL_RETRY_BASE_SECONDS", 0.01)
    monkeypatch.setattr(agent, "ENROLL_RETRY_MAX_SECONDS", 0.02)

    with (
        caplog.at_level(logging.WARNING, logger=agent.__name__),
        pytest.raises(httpx.ConnectError),
    ):
        await agent.self_enroll(
            f"http://127.0.0.1:{closed_port}", BOOTSTRAP_TOKEN, "node-1", "node-1"
        )

    retries = [r for r in caplog.records if r.msg.startswith("worker_enroll_retrying")]
    assert [r.getMessage().split(" error=")[0] for r in retries] == [
        "worker_enroll_retrying retry_in_seconds=0.01",
        "worker_enroll_retrying retry_in_seconds=0.02",
    ]
    assert all(" error=ConnectError: " in r.getMessage() for r in retries)
    assert all(r.exc_info is None for r in retries)
