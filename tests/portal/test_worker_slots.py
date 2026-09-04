from __future__ import annotations

from typing import TYPE_CHECKING

from portal.repository.workers import PostgresWorkerRegistry
from portal.security import new_worker_credential

from tests.portal.conftest import enroll_worker


if TYPE_CHECKING:
    import asyncpg

    from litestar.testing import AsyncTestClient


async def _headers_for(pool: asyncpg.Pool, worker_id: str) -> dict[str, str]:
    credential = new_worker_credential()
    await PostgresWorkerRegistry(pool).issue(
        worker_id, credential, f"{worker_id}.tailnet.ts.net"
    )
    return {"Authorization": f"Bearer {credential}", "X-Portal-Worker": worker_id}


async def test_claim_slot_requires_an_authorized_worker(
    worker_client: AsyncTestClient,
) -> None:
    response = await worker_client.post(
        "/claim-slot", json={"provider": "geonode", "lane_index": 0}
    )

    assert response.status_code == 403


async def test_claim_slot_returns_a_geonode_port_slot(
    pool: asyncpg.Pool, worker_client: AsyncTestClient
) -> None:
    headers = await enroll_worker(pool)

    response = await worker_client.post(
        "/claim-slot",
        json={"provider": "geonode", "lane_index": 0},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {"slot_id": 1}


async def test_two_lanes_claiming_concurrently_get_different_slots(
    pool: asyncpg.Pool, worker_client: AsyncTestClient
) -> None:
    a_headers = await _headers_for(pool, "trabajador-a")
    b_headers = await _headers_for(pool, "trabajador-b")

    a_response = await worker_client.post(
        "/claim-slot",
        json={"provider": "geonode", "lane_index": 0},
        headers=a_headers,
    )
    b_response = await worker_client.post(
        "/claim-slot",
        json={"provider": "geonode", "lane_index": 0},
        headers=b_headers,
    )

    assert a_response.json()["slot_id"] != b_response.json()["slot_id"]


async def test_release_slot_frees_it_for_the_next_claim(
    pool: asyncpg.Pool, worker_client: AsyncTestClient
) -> None:
    headers = await enroll_worker(pool)

    claimed = await worker_client.post(
        "/claim-slot",
        json={"provider": "geonode", "lane_index": 0},
        headers=headers,
    )
    slot_id = claimed.json()["slot_id"]

    released = await worker_client.post(
        "/release-slot",
        json={"provider": "geonode", "slot_id": slot_id},
        headers=headers,
    )
    assert released.status_code == 204

    row = await pool.fetchrow(
        "SELECT worker_id FROM portal_proxy_slots "
        "WHERE provider = 'geonode' AND slot_id = $1",
        slot_id,
    )
    assert row is not None
    assert row["worker_id"] is None


async def test_release_slot_by_a_worker_that_does_not_hold_it_is_a_safe_no_op(
    pool: asyncpg.Pool, worker_client: AsyncTestClient
) -> None:
    a_headers = await _headers_for(pool, "trabajador-a")
    b_headers = await _headers_for(pool, "trabajador-b")

    claimed = await worker_client.post(
        "/claim-slot",
        json={"provider": "geonode", "lane_index": 0},
        headers=a_headers,
    )
    slot_id = claimed.json()["slot_id"]

    response = await worker_client.post(
        "/release-slot",
        json={"provider": "geonode", "slot_id": slot_id},
        headers=b_headers,
    )
    assert response.status_code == 204

    row = await pool.fetchrow(
        "SELECT worker_id FROM portal_proxy_slots "
        "WHERE provider = 'geonode' AND slot_id = $1",
        slot_id,
    )
    assert row is not None
    assert row["worker_id"] == "trabajador-a"


async def test_heartbeat_renews_a_held_slots_lease(
    pool: asyncpg.Pool, worker_client: AsyncTestClient
) -> None:
    headers = await enroll_worker(pool)

    claimed = await worker_client.post(
        "/claim-slot",
        json={"provider": "geonode", "lane_index": 0},
        headers=headers,
    )
    slot_id = claimed.json()["slot_id"]

    await pool.execute(
        "UPDATE portal_proxy_slots SET lease_expires_at = now() + interval '1 second' "
        "WHERE provider = 'geonode' AND slot_id = $1",
        slot_id,
    )

    response = await worker_client.post(
        "/heartbeat",
        json={"held_slots": [{"provider": "geonode", "slot_id": slot_id}]},
        headers=headers,
    )
    assert response.status_code == 204

    still_fresh = await pool.fetchval(
        "SELECT lease_expires_at > now() + interval '30 seconds' "
        "FROM portal_proxy_slots WHERE provider = 'geonode' AND slot_id = $1",
        slot_id,
    )
    assert still_fresh


async def test_heartbeat_without_reporting_a_held_slot_does_not_renew_it(
    pool: asyncpg.Pool, worker_client: AsyncTestClient
) -> None:
    """A slot this worker actually holds but doesn't list is exactly what a
    freshly restarted process reports for its predecessor's leaked rows: no
    renewal, so the lease keeps counting down instead of being masked."""
    headers = await enroll_worker(pool)

    claimed = await worker_client.post(
        "/claim-slot",
        json={"provider": "geonode", "lane_index": 0},
        headers=headers,
    )
    slot_id = claimed.json()["slot_id"]

    before = await pool.fetchval(
        "SELECT lease_expires_at FROM portal_proxy_slots "
        "WHERE provider = 'geonode' AND slot_id = $1",
        slot_id,
    )

    response = await worker_client.post("/heartbeat", json={}, headers=headers)
    assert response.status_code == 204

    after = await pool.fetchval(
        "SELECT lease_expires_at FROM portal_proxy_slots "
        "WHERE provider = 'geonode' AND slot_id = $1",
        slot_id,
    )
    assert after == before
