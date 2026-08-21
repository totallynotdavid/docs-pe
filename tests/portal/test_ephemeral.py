from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    import asyncpg

    from portal.ephemeral import EphemeralStore


LONG = timedelta(minutes=5)
EXPIRED = timedelta(seconds=-1)


async def test_a_key_is_held_until_it_expires(store: EphemeralStore) -> None:
    assert await store.put_new("clave", "primero", LONG) is True
    assert await store.put_new("clave", "segundo", LONG) is False
    assert await store.read("clave") == "primero"


async def test_an_expired_key_reads_as_missing_and_can_be_taken_over(
    store: EphemeralStore,
) -> None:
    await store.put_new("clave", "viejo", EXPIRED)

    assert await store.read("clave") is None
    assert await store.take("clave") is None
    assert await store.replace("clave", "nuevo", LONG) is False

    # An expired row must not block the key: a session token that lapsed is
    # free for the next holder in the same statement that writes it.
    assert await store.put_new("clave", "nuevo", LONG) is True
    assert await store.read("clave") == "nuevo"


async def test_taking_a_value_spends_it(store: EphemeralStore) -> None:
    await store.put_new("token", "carga", LONG)

    assert await store.take("token") == "carga"
    assert await store.take("token") is None


async def test_a_counter_window_starts_at_the_first_event(
    store: EphemeralStore,
    pool: asyncpg.Pool,
) -> None:
    for expected in (1, 2, 3):
        assert await store.increment("contador", LONG) == expected

    deadline = await pool.fetchval(
        "SELECT expires_at FROM portal_ephemeral WHERE key = 'contador'"
    )

    await store.increment("contador", timedelta(hours=1))

    # A later event with a longer TTL must not push the deadline out, or a
    # steady stream of attempts would turn a soft lock into a permanent one.
    assert (
        await pool.fetchval(
            "SELECT expires_at FROM portal_ephemeral WHERE key = 'contador'"
        )
        == deadline
    )


async def test_a_counter_restarts_once_its_window_has_passed(
    store: EphemeralStore,
) -> None:
    await store.increment("contador", EXPIRED)

    assert await store.increment("contador", LONG) == 1


async def test_the_sweep_removes_only_what_has_expired(
    store: EphemeralStore,
    pool: asyncpg.Pool,
) -> None:
    await store.put_new("viva", "x", LONG)
    await store.put_new("vencida", "x", EXPIRED)

    assert await store.sweep() == 1
    assert await pool.fetchval("SELECT count(*) FROM portal_ephemeral") == 1
    assert await store.read("viva") == "x"
