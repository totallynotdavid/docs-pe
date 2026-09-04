from __future__ import annotations

from typing import TYPE_CHECKING

from core.pipeline.breaker import DEFAULT_THRESHOLD
from portal.repository.breakers import PostgresCircuitBreakers


if TYPE_CHECKING:
    import asyncpg


async def _row(pool: asyncpg.Pool) -> asyncpg.Record:
    row = await pool.fetchrow(
        "SELECT consecutive_failures, level, open_until "
        "FROM portal_circuit_breakers WHERE source = 'osiptel' AND provider = 'geonode'"
    )
    assert row is not None
    return row


async def test_a_success_resets_a_fresh_pair_to_closed(pool: asyncpg.Pool) -> None:
    breakers = PostgresCircuitBreakers(pool)

    await breakers.record_outcome(
        source="osiptel", provider="geonode", healthy_contact=True
    )

    row = await _row(pool)
    assert row["consecutive_failures"] == 0
    assert row["level"] == 0
    assert row["open_until"] is None


async def test_failures_below_threshold_accumulate_without_opening(
    pool: asyncpg.Pool,
) -> None:
    breakers = PostgresCircuitBreakers(pool)

    for _ in range(DEFAULT_THRESHOLD - 1):
        await breakers.record_outcome(
            source="osiptel", provider="geonode", healthy_contact=False
        )

    row = await _row(pool)
    assert row["consecutive_failures"] == DEFAULT_THRESHOLD - 1
    assert row["open_until"] is None


async def test_the_threshold_th_consecutive_failure_opens_the_pair(
    pool: asyncpg.Pool,
) -> None:
    breakers = PostgresCircuitBreakers(pool)

    for _ in range(DEFAULT_THRESHOLD):
        await breakers.record_outcome(
            source="osiptel", provider="geonode", healthy_contact=False
        )

    row = await _row(pool)
    assert row["consecutive_failures"] == 0
    assert row["level"] == 1
    assert row["open_until"] is not None


async def test_a_success_closes_an_open_pair(pool: asyncpg.Pool) -> None:
    breakers = PostgresCircuitBreakers(pool)

    for _ in range(DEFAULT_THRESHOLD):
        await breakers.record_outcome(
            source="osiptel", provider="geonode", healthy_contact=False
        )

    await breakers.record_outcome(
        source="osiptel", provider="geonode", healthy_contact=True
    )

    row = await _row(pool)
    assert row["consecutive_failures"] == 0
    assert row["level"] == 0
    assert row["open_until"] is None


async def test_a_second_trip_escalates_the_cooldown_level(pool: asyncpg.Pool) -> None:
    breakers = PostgresCircuitBreakers(pool)

    for _ in range(DEFAULT_THRESHOLD):
        await breakers.record_outcome(
            source="osiptel", provider="geonode", healthy_contact=False
        )
    for _ in range(DEFAULT_THRESHOLD):
        await breakers.record_outcome(
            source="osiptel", provider="geonode", healthy_contact=False
        )

    row = await _row(pool)
    assert row["level"] == 2


async def test_different_source_provider_pairs_are_independent(
    pool: asyncpg.Pool,
) -> None:
    breakers = PostgresCircuitBreakers(pool)

    for _ in range(DEFAULT_THRESHOLD):
        await breakers.record_outcome(
            source="osiptel", provider="geonode", healthy_contact=False
        )
    await breakers.record_outcome(
        source="sunat", provider="geonode", healthy_contact=False
    )

    osiptel_row = await _row(pool)
    sunat_row = await pool.fetchrow(
        "SELECT consecutive_failures, open_until FROM portal_circuit_breakers "
        "WHERE source = 'sunat' AND provider = 'geonode'"
    )

    assert osiptel_row["open_until"] is not None
    assert sunat_row is not None
    assert sunat_row["consecutive_failures"] == 1
    assert sunat_row["open_until"] is None
