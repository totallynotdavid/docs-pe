from __future__ import annotations

import dataclasses

from typing import TYPE_CHECKING

import asyncpg
import pytest

from litestar.testing import AsyncTestClient
from portal.domain.errors import PermissionDenied
from portal.repository.jobs import PostgresJobRepository
from portal.repository.workers import PostgresWorkerRegistry, worker_role_name
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
        await connection.execute(
            f'DROP ROLE IF EXISTS "{worker_role_name("poseidon-1")}"'
        )
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
        json={"worker_id": "poseidon-1", "tailscale_hostname": "poseidon.tailnet.test"},
        headers={"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"},
    )

    assert response.status_code == 200
    dsn = response.json()["database_dsn"]
    role = worker_role_name("poseidon-1")
    assert f"{role}:" in dsn
    assert dsn != settings.database_dsn

    worker_pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)

    try:
        claimed = await PostgresJobRepository(worker_pool).claim_many(
            "poseidon-1", ("osiptel",), 1
        )
        assert claimed == ()

        with pytest.raises(asyncpg.InsufficientPrivilegeError):
            await worker_pool.fetchval(
                "SELECT config_ciphertext FROM "
                "portal_team_proxy_credential_versions LIMIT 1"
            )
    finally:
        await worker_pool.close()


async def test_re_enrolling_re_keys_the_direct_db_role_idempotently(
    bootstrapped_client: AsyncTestClient,
) -> None:
    """Re-enrollment rotates the worker's Postgres login password."""
    headers = {"Authorization": f"Bearer {BOOTSTRAP_TOKEN}"}
    body = {"worker_id": "poseidon-1", "tailscale_hostname": "poseidon.tailnet.test"}

    first = await bootstrapped_client.post("/enroll", json=body, headers=headers)
    second = await bootstrapped_client.post("/enroll", json=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["database_dsn"] != second.json()["database_dsn"]

    worker_pool = await asyncpg.create_pool(
        second.json()["database_dsn"], min_size=1, max_size=1
    )
    await worker_pool.close()
