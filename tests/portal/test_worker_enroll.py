from __future__ import annotations

import dataclasses

from typing import TYPE_CHECKING

import pytest

from litestar.testing import AsyncTestClient
from portal.domain.errors import PermissionDenied
from portal.repository.workers import PostgresWorkerRegistry
from portal.worker.api import create_worker_api


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import asyncpg

    from litestar import Litestar
    from portal.settings import PortalSettings


BOOTSTRAP_TOKEN = "bootstrap-secret"


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
    """The base `settings` fixture leaves worker_bootstrap_token empty, matching
    a deployment that never set PORTAL_WORKER_BOOTSTRAP_TOKEN. An empty bearer
    must not compare equal to an empty configured token."""
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
    """A restarted node re-enrolls instead of reusing a persisted credential:
    the second call must re-key the same worker_id, not create a second one."""
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
