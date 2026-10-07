from __future__ import annotations

from typing import TYPE_CHECKING

from core.pipeline.breaker import DEFAULT_THRESHOLD
from portal.repository.breakers import PostgresCircuitBreakers

from tests.portal.conftest import build_experience, login, sync_client


if TYPE_CHECKING:
    import asyncpg

    from litestar import Litestar
    from portal.repository.teams import PostgresTeamRepository


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


async def test_states_report_which_pairs_are_open(pool: asyncpg.Pool) -> None:
    breakers = PostgresCircuitBreakers(pool)
    for _ in range(DEFAULT_THRESHOLD):
        await breakers.record_outcome(
            source="osiptel", provider="geonode", healthy_contact=False
        )
    await breakers.record_outcome(
        source="sunat", provider="geonode", healthy_contact=False
    )

    osiptel, sunat = await breakers.states()

    assert (osiptel.source, osiptel.provider) == ("osiptel", "geonode")
    assert osiptel.is_open
    assert osiptel.open_until is not None
    assert (sunat.source, sunat.consecutive_failures) == ("sunat", 1)
    assert not sunat.is_open


async def test_system_page_shows_an_open_pair_to_a_site_admin(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    await build_experience(pool, team_repository)
    breakers = PostgresCircuitBreakers(pool)
    for _ in range(DEFAULT_THRESHOLD):
        await breakers.record_outcome(
            source="osiptel", provider="geonode", healthy_contact=False
        )

    with sync_client(app) as client:
        assert login(client, "admin@osiptel.test").status_code == 303
        page = client.get("/admin/system")

    assert page.status_code == 200
    assert "geonode" in page.text
    assert "Bloqueado hasta" in page.text
