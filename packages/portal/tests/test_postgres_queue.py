from __future__ import annotations

import asyncio
import os

from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import asyncpg
import pytest

from portal.application.service import PortalService
from portal.domain.models import InputLine, JobState, SubmitJob
from portal.migrations import apply_migrations
from portal.repository.postgres import PostgresPortalRepository


POSTGRES_DSN = os.environ.get("PORTAL_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not POSTGRES_DSN, reason="set PORTAL_TEST_DSN to run PostgreSQL queue contracts"
)


async def test_postgresql_gate_limits_concurrent_processes_and_preserves_results() -> (
    None
):
    """Exercise the real FOR UPDATE gate, FIFO promotion, and cancellation fence."""
    database = f"portal_contract_{uuid4().hex}"
    maintenance_dsn, test_dsn = _database_dsns(POSTGRES_DSN, database)
    maintenance = await asyncpg.connect(maintenance_dsn)
    await maintenance.execute(f'CREATE DATABASE "{database}"')
    await maintenance.close()
    try:
        pool = await asyncpg.create_pool(test_dsn, min_size=1, max_size=12)
        try:
            await apply_migrations(pool)
            actor_id, team_id, credential_id = await _seed_team(pool)
            service = PortalService(PostgresPortalRepository(pool))
            commands = [
                SubmitJob(
                    actor_id=actor_id,
                    team_id=team_id,
                    credential_version_id=credential_id,
                    input_object_id=await _input_reference(pool, team_id, number),
                    filename=f"entrada-{number}.csv",
                    sources=("osiptel",),
                    lines=(InputLine(1, "10412345678"),),
                )
                for number in range(10)
            ]
            jobs = await asyncio.gather(
                *(service.submit(command) for command in commands)
            )
            repository = PostgresPortalRepository(pool)
            service = PortalService(repository)
            running = [job for job in jobs if job.state is JobState.RUNNING]
            queued = sorted(
                (job for job in jobs if job.state is JobState.QUEUED),
                key=lambda job: job.queue_sequence,
            )
            assert len(running) == 5
            assert [job.queue_sequence for job in queued] == [6, 7, 8, 9, 10]

            claimed = await repository.claim("trabajador-prueba", ("osiptel",))
            assert claimed is not None
            partial_job = next(job for job in running if job.id != claimed.job_id)
            result_reference = await _result_reference(pool, team_id)
            await pool.execute(
                """
                UPDATE portal_job_items
                   SET state = 'published', result_object_id = $1, published_at = now()
                 WHERE job_id = $2
                """,
                result_reference,
                partial_job.id,
            )
            cancelled = await service.cancel(actor_id, team_id, claimed.job_id)
            assert cancelled.state is JobState.CANCELLED
            assert (
                await repository.publish(
                    claimed.item_id,
                    "trabajador-prueba",
                    claimed.lease_fence,
                    result_reference,
                )
                is False
            )
            assert queued[0].id == await pool.fetchval(
                """
                SELECT id FROM portal_jobs
                 WHERE state = 'running'
                 ORDER BY queue_sequence DESC
                 LIMIT 1
                """
            )
            assert (
                await pool.fetchval(
                    """
                    SELECT result_object_id FROM portal_job_items
                     WHERE job_id = $1 AND state = 'published'
                    """,
                    partial_job.id,
                )
                == result_reference
            )
            partial_cancelled = await service.cancel(actor_id, team_id, partial_job.id)
            assert partial_cancelled.state is JobState.CANCELLED
            assert (
                await pool.fetchval("SELECT count(*) FROM portal_notification_outbox")
                == 6
            )
        finally:
            await pool.close()
    finally:
        maintenance = await asyncpg.connect(maintenance_dsn)
        await maintenance.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await maintenance.close()


def _database_dsns(dsn: str, database: str) -> tuple[str, str]:
    parts = urlsplit(dsn)
    maintenance = urlunsplit(parts._replace(path="/postgres"))
    test = urlunsplit(parts._replace(path=f"/{database}"))
    return maintenance, test


async def _seed_team(pool: asyncpg.Pool) -> tuple[UUID, UUID, UUID]:
    actor_id, team_id, credential_id = uuid4(), uuid4(), uuid4()
    credential_root_id = uuid4()
    async with pool.acquire() as connection:
        await connection.execute(
            "INSERT INTO portal_users (id, email, password_hash) VALUES ($1, $2, 'x')",
            actor_id,
            f"{actor_id.hex}@example.test",
        )
        await connection.execute(
            "INSERT INTO portal_teams (id, slug, name, created_by) VALUES ($1, $2, 'Equipo', $3)",
            team_id,
            f"equipo-{team_id.hex[:10]}",
            actor_id,
        )
        await connection.execute(
            """
            INSERT INTO portal_team_memberships (team_id, user_id, role)
            VALUES ($1, $2, 'team_leader')
            """,
            team_id,
            actor_id,
        )
        await connection.execute(
            """
            INSERT INTO portal_team_proxy_credentials (id, team_id, label, created_by)
            VALUES ($1, $2, 'Proxy Perú', $3)
            """,
            credential_root_id,
            team_id,
            actor_id,
        )
        await connection.execute(
            """
            INSERT INTO portal_team_proxy_credential_versions
                (id, credential_id, team_id, version, provider, config_ciphertext, key_id, created_by)
            VALUES ($1, $2, $3, 1, 'prueba', $4, 'clave-prueba', $5)
            """,
            credential_id,
            credential_root_id,
            team_id,
            b"cifrado",
            actor_id,
        )
    return actor_id, team_id, credential_id


async def _input_reference(pool: asyncpg.Pool, team_id: UUID, number: int) -> UUID:
    return await _reference(pool, team_id, f"entradas/{number}.csv")


async def _result_reference(pool: asyncpg.Pool, team_id: UUID) -> UUID:
    return await _reference(pool, team_id, "resultados/uno.json")


async def _reference(pool: asyncpg.Pool, team_id: UUID, key: str) -> UUID:
    reference_id = uuid4()
    await pool.execute(
        """
        INSERT INTO portal_object_references
            (id, team_id, provider, container, object_key, sha256, size_bytes, content_type)
        VALUES ($1, $2, 'prueba', 'portal', $3, $4, 1, 'text/csv')
        """,
        reference_id,
        team_id,
        key,
        "0" * 64,
    )
    return reference_id
