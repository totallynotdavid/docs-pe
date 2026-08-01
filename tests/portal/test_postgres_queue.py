from __future__ import annotations

import asyncio
import base64
import os

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import asyncpg
import httpx
import pytest

from portal.application.service import PortalService
from portal.credentials.secrets import DevelopmentAesGcmSecretProtector
from portal.domain.models import (
    MAX_LEASE_ATTEMPTS,
    InputLine,
    Job,
    JobState,
    SubmitJob,
)
from portal.migrations import apply_migrations
from portal.repository.postgres import PostgresPortalRepository
from portal.settings import PortalSettings
from portal.web.app import create_app


if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastapi import FastAPI


POSTGRES_DSN = os.environ.get("PORTAL_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not POSTGRES_DSN, reason="set PORTAL_TEST_DSN to run PostgreSQL queue contracts"
)


@asynccontextmanager
async def _portal_database(prefix: str) -> AsyncIterator[asyncpg.Pool]:
    """Give one test a migrated database of its own, dropped afterwards."""
    database = f"portal_{prefix}_{uuid4().hex}"
    maintenance_dsn, test_dsn = _database_dsns(POSTGRES_DSN, database)
    maintenance = await asyncpg.connect(maintenance_dsn)
    await maintenance.execute(f'CREATE DATABASE "{database}"')
    await maintenance.close()
    try:
        pool = await asyncpg.create_pool(test_dsn, min_size=1, max_size=12)
        try:
            await apply_migrations(pool)
            yield pool
        finally:
            await pool.close()
    finally:
        maintenance = await asyncpg.connect(maintenance_dsn)
        await maintenance.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        await maintenance.close()


@asynccontextmanager
async def _portal_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Drive the app on the test's own event loop.

    `TestClient` runs the app in a second loop, and an asyncpg pool belongs to
    the loop that created it, so the two cannot share one database.
    """
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        yield client


async def _expire_leases(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """
        UPDATE portal_job_items
           SET lease_expires_at = now() - interval '1 minute'
         WHERE state = 'running'
        """
    )


async def test_postgresql_gate_limits_concurrent_processes_and_preserves_results() -> (
    None
):
    """Exercise the real FOR UPDATE gate, FIFO promotion, and cancellation fence."""
    async with _portal_database("contract") as pool:
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
        jobs = await asyncio.gather(*(service.submit(command) for command in commands))
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
            await pool.fetchval("SELECT count(*) FROM portal_notification_outbox") == 6
        )


async def test_postgresql_login_limit_uses_a_timestamp_window() -> None:
    async with _portal_database("login") as pool:
        repository = PostgresPortalRepository(pool)
        now = datetime.now(UTC)
        assert await repository.login_allowed("persona@example.test", "127.0.0.1", now)


async def test_an_expired_lease_returns_its_item_to_the_queue() -> None:
    """A worker that stops renewing must not strand its item in 'running'."""
    async with _portal_database("lease") as pool:
        repository = PostgresPortalRepository(pool)
        await _submit_one(pool, repository)

        first = await repository.claim("trabajador-uno", ("osiptel",))
        assert first is not None
        assert await _attempts(pool, first.item_id) == 1

        await _expire_leases(pool)
        second = await repository.claim("trabajador-dos", ("osiptel",))

        assert second is not None
        assert second.item_id == first.item_id
        assert await _attempts(pool, second.item_id) == 2


async def test_a_repeatedly_expired_item_retires_and_fails_its_job() -> None:
    """The cap stops an item cycling forever, and an empty job is not 'completed'."""
    async with _portal_database("retire") as pool:
        repository = PostgresPortalRepository(pool)
        job = await _submit_one(pool, repository)

        for _ in range(MAX_LEASE_ATTEMPTS):
            assert await repository.claim("trabajador", ("osiptel",)) is not None
            await _expire_leases(pool)

        assert await repository.claim("trabajador", ("osiptel",)) is None
        item = await pool.fetchrow(
            "SELECT state, reason FROM portal_job_items WHERE job_id = $1", job.id
        )
        assert item["state"] == "failed"
        assert item["reason"] == "lease_expired"

        finished = await pool.fetchrow(
            "SELECT state, terminal_reason FROM portal_jobs WHERE id = $1", job.id
        )
        assert finished["state"] == "failed"
        assert finished["terminal_reason"] == "sin_resultados"


async def test_published_search_finds_a_dni_inside_a_ruc_and_paginates() -> None:
    """A RUC-10 embeds its owner's DNI, so a DNI search must return both rows."""
    async with _portal_database("search") as pool:
        repository = PostgresPortalRepository(pool)
        job = await _submit_one(pool, repository)
        reference = await _result_reference(pool, team_id=job.team_id)
        await pool.execute(
            """
            UPDATE portal_job_items
               SET document = '10123456789', state = 'published',
                   result_object_id = $2, published_at = now()
             WHERE job_id = $1
            """,
            job.id,
            reference,
        )
        await pool.execute(
            """
            INSERT INTO portal_job_items
                (id, job_id, team_id, ordinal, document, source, state,
                 result_object_id, published_at)
            VALUES ($1, $2, $3, 2, '12345678', 'osiptel', 'published', $4, now())
            """,
            uuid4(),
            job.id,
            job.team_id,
            reference,
        )

        found, more = await repository.search_published(
            job.team_id, "12345678", limit=20, offset=0
        )
        assert {result.document for result in found} == {"10123456789", "12345678"}
        assert more is False

        first_page, more = await repository.search_published(
            job.team_id, "12345678", limit=1, offset=0
        )
        assert len(first_page) == 1
        assert more is True


async def test_the_worker_api_leases_an_item_and_publishes_its_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker's only view of the queue is this pair of authenticated calls."""
    monkeypatch.setenv("PORTAL_WORKER_BOOTSTRAP_TOKEN", "ficha-de-prueba")
    protector = DevelopmentAesGcmSecretProtector(
        base64.urlsafe_b64encode(b"c" * 32).decode("ascii")
    )
    secret = await protector.protect({"username": "equipo", "password": "clave"})
    async with _portal_database("worker") as pool:
        repository = PostgresPortalRepository(pool)
        job = await _submit_one(pool, repository, ciphertext=secret.ciphertext)
        app = create_app(
            PortalSettings(""), repository=repository, secret_protector=protector
        )
        headers = {
            "Authorization": "Bearer ficha-de-prueba",
            "X-Portal-Worker": "trabajador-uno",
        }
        async with _portal_client(app) as client:
            anonymous = await client.post("/api/worker/claim", json={"sources": []})
            assert anonymous.status_code == 401

            response = await client.post(
                "/api/worker/claim", json={"sources": ["osiptel"]}, headers=headers
            )
            claimed = response.json()
            assert claimed["document"] == "10412345678"
            assert claimed["credential"]["config"] == {
                "username": "equipo",
                "password": "clave",
            }

            published = await client.post(
                "/api/worker/publish",
                json={
                    "item_id": claimed["item_id"],
                    "fence": claimed["fence"],
                    "content": base64.b64encode(b'{"lineas": []}').decode("ascii"),
                },
                headers=headers,
            )

        assert published.json() == {"published": True}
        finished = await pool.fetchrow(
            "SELECT state FROM portal_jobs WHERE id = $1", job.id
        )
        assert finished["state"] == "completed"


async def _submit_one(
    pool: asyncpg.Pool,
    repository: PostgresPortalRepository,
    *,
    ciphertext: bytes = b"cifrado",
) -> Job:
    """Seed a team and admit a single running job holding one osiptel item."""
    actor_id, team_id, credential_id = await _seed_team(pool, ciphertext=ciphertext)
    return await PortalService(repository).submit(
        SubmitJob(
            actor_id=actor_id,
            team_id=team_id,
            credential_version_id=credential_id,
            input_object_id=await _input_reference(pool, team_id, 0),
            filename="entrada.csv",
            sources=("osiptel",),
            lines=(InputLine(1, "10412345678"),),
        )
    )


async def _attempts(pool: asyncpg.Pool, item_id: UUID) -> int:
    return int(
        await pool.fetchval(
            "SELECT attempts FROM portal_job_items WHERE id = $1", item_id
        )
    )


def _database_dsns(dsn: str, database: str) -> tuple[str, str]:
    parts = urlsplit(dsn)
    maintenance = urlunsplit(parts._replace(path="/postgres"))
    test = urlunsplit(parts._replace(path=f"/{database}"))
    return maintenance, test


async def _seed_team(
    pool: asyncpg.Pool, *, ciphertext: bytes = b"cifrado"
) -> tuple[UUID, UUID, UUID]:
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
                (id, credential_id, team_id, version, provider, config_ciphertext,
                 key_id, lifecycle, is_active, created_by)
            VALUES ($1, $2, $3, 1, 'geonode', $4, 'clave-prueba', 'active', true, $5)
            """,
            credential_id,
            credential_root_id,
            team_id,
            ciphertext,
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
