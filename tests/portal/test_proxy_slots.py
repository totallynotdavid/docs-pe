from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from portal.repository.slots import PostgresProxySlots
from portal.repository.workers import PostgresWorkerRegistry
from portal.security import new_worker_credential


if TYPE_CHECKING:
    import asyncpg


async def _row(pool: asyncpg.Pool, slot_id: int) -> asyncpg.Record:
    row = await pool.fetchrow(
        "SELECT worker_id, lane_index, leased_at FROM portal_proxy_slots "
        "WHERE provider = 'geonode' AND slot_id = $1",
        slot_id,
    )
    assert row is not None
    return row


async def _issue(pool: asyncpg.Pool, worker_id: str) -> None:
    await PostgresWorkerRegistry(pool).issue(
        worker_id, new_worker_credential(), "host.tailnet"
    )


async def test_claim_leases_the_lowest_free_slot(pool: asyncpg.Pool) -> None:
    await _issue(pool, "trabajador")
    slots = PostgresProxySlots(pool)

    slot_id = await slots.claim(
        provider="geonode", worker_id="trabajador", lane_index=0
    )

    assert slot_id == 1
    row = await _row(pool, 1)
    assert row["worker_id"] == "trabajador"
    assert row["lane_index"] == 0
    assert row["leased_at"] is not None


async def test_concurrent_claims_never_collide(pool: asyncpg.Pool) -> None:
    for i in range(20):
        await _issue(pool, f"trabajador-{i}")
    slots = PostgresProxySlots(pool)

    results = await asyncio.gather(
        *(
            slots.claim(provider="geonode", worker_id=f"trabajador-{i}", lane_index=0)
            for i in range(20)
        )
    )

    assert all(result is not None for result in results)
    assert len(set(results)) == 20


async def test_release_frees_the_slot_for_someone_else(pool: asyncpg.Pool) -> None:
    await _issue(pool, "a")
    await _issue(pool, "b")
    slots = PostgresProxySlots(pool)

    first = await slots.claim(provider="geonode", worker_id="a", lane_index=0)
    assert first is not None

    await slots.release(provider="geonode", slot_id=first, worker_id="a")

    row = await _row(pool, first)
    assert row["worker_id"] is None
    assert row["leased_at"] is None

    second = await slots.claim(provider="geonode", worker_id="b", lane_index=0)
    assert second == first


async def test_release_with_the_wrong_worker_id_is_a_no_op(pool: asyncpg.Pool) -> None:
    await _issue(pool, "a")
    await _issue(pool, "b")
    slots = PostgresProxySlots(pool)

    claimed = await slots.claim(provider="geonode", worker_id="a", lane_index=0)
    assert claimed is not None

    await slots.release(provider="geonode", slot_id=claimed, worker_id="b")

    row = await _row(pool, claimed)
    assert row["worker_id"] == "a"


async def test_a_stale_workers_slot_is_reclaimable_without_release(
    pool: asyncpg.Pool,
) -> None:
    await _issue(pool, "gone")
    await _issue(pool, "here")
    slots = PostgresProxySlots(pool)

    claimed = await slots.claim(provider="geonode", worker_id="gone", lane_index=0)
    assert claimed is not None

    await pool.execute(
        "UPDATE portal_workers SET last_seen_at = now() - interval '1 hour' "
        "WHERE worker_id = 'gone'"
    )

    reclaimed = await slots.claim(provider="geonode", worker_id="here", lane_index=0)

    assert reclaimed == claimed
    row = await _row(pool, claimed)
    assert row["worker_id"] == "here"


async def test_a_revoked_workers_slot_is_reclaimable(pool: asyncpg.Pool) -> None:
    await _issue(pool, "gone")
    await _issue(pool, "here")
    slots = PostgresProxySlots(pool)

    claimed = await slots.claim(provider="geonode", worker_id="gone", lane_index=0)
    assert claimed is not None

    await PostgresWorkerRegistry(pool).revoke("gone")

    reclaimed = await slots.claim(provider="geonode", worker_id="here", lane_index=0)
    assert reclaimed == claimed


async def test_a_slot_held_by_a_worker_that_never_heartbeat_frees_after_the_window(
    pool: asyncpg.Pool,
) -> None:
    """`issue()` leaves last_seen_at NULL until the first heartbeat, so a
    worker that crashes before sending one is judged by how long ago its slot
    was leased instead."""
    await _issue(pool, "gone")
    await _issue(pool, "here")
    slots = PostgresProxySlots(pool)

    claimed = await slots.claim(provider="geonode", worker_id="gone", lane_index=0)
    assert claimed is not None

    await pool.execute(
        "UPDATE portal_proxy_slots SET leased_at = now() - interval '1 hour' "
        "WHERE provider = 'geonode' AND slot_id = $1",
        claimed,
    )

    reclaimed = await slots.claim(provider="geonode", worker_id="here", lane_index=0)
    assert reclaimed == claimed


async def test_claim_returns_none_once_every_slot_is_held_by_a_live_worker(
    pool: asyncpg.Pool,
) -> None:
    await _issue(pool, "hog")
    slots = PostgresProxySlots(pool)

    await pool.execute(
        "UPDATE portal_proxy_slots SET worker_id = 'hog', lane_index = 0, "
        "leased_at = now() WHERE provider = 'geonode'"
    )

    result = await slots.claim(
        provider="geonode", worker_id="someone-else", lane_index=0
    )

    assert result is None
