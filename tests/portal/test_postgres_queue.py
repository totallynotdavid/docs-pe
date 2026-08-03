from __future__ import annotations

import asyncio
import base64

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from portal.domain.models import (
    MAX_LEASE_ATTEMPTS,
    InputLine,
    Job,
    JobState,
    SubmitJob,
)

from tests.portal.conftest import WORKER_TOKEN, object_reference, seed_team


if TYPE_CHECKING:
    import asyncpg
    import httpx

    from portal.application.service import PortalService
    from portal.credentials.secrets import AesGcmSecretProtector
    from portal.repository.jobs import PostgresJobRepository


async def _expire_leases(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """
        UPDATE portal_job_items
           SET lease_expires_at = now() - interval '1 minute'
         WHERE state = 'running'
        """
    )


async def test_postgresql_gate_limits_concurrent_processes_and_preserves_results(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    """Exercise the real FOR UPDATE gate, FIFO promotion, and cancellation fence."""
    actor_id, team_id, credential_id = await seed_team(pool)
    commands = [
        SubmitJob(
            actor_id=actor_id,
            team_id=team_id,
            credential_version_id=credential_id,
            input_object_id=await object_reference(
                pool, team_id, f"entradas/{number}.csv"
            ),
            filename=f"entrada-{number}.csv",
            sources=("osiptel",),
            lines=(InputLine(1, "10412345678"),),
        )
        for number in range(10)
    ]
    jobs = await asyncio.gather(*(service.submit(command) for command in commands))
    running = [job for job in jobs if job.state is JobState.RUNNING]
    queued = sorted(
        (job for job in jobs if job.state is JobState.QUEUED),
        key=lambda job: job.queue_sequence,
    )
    assert len(running) == 5
    assert [job.queue_sequence for job in queued] == [6, 7, 8, 9, 10]

    claimed = await job_repository.claim("trabajador-prueba", ("osiptel",))
    assert claimed is not None
    partial_job = next(job for job in running if job.id != claimed.job_id)
    result_reference = await object_reference(pool, team_id, "resultados/uno.json")
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
        await job_repository.publish(
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
    assert await pool.fetchval("SELECT count(*) FROM portal_notification_outbox") == 6


async def test_an_expired_lease_returns_its_item_to_the_queue(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    """A worker that stops renewing must not strand its item in 'running'."""
    await _submit_one(pool, service)

    first = await job_repository.claim("trabajador-uno", ("osiptel",))
    assert first is not None
    assert await _attempts(pool, first.item_id) == 1

    await _expire_leases(pool)
    second = await job_repository.claim("trabajador-dos", ("osiptel",))

    assert second is not None
    assert second.item_id == first.item_id
    assert await _attempts(pool, second.item_id) == 2


async def test_a_repeatedly_expired_item_retires_and_fails_its_job(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    """The cap stops an item cycling forever, and an empty job is not 'completed'."""
    job = await _submit_one(pool, service)

    for _ in range(MAX_LEASE_ATTEMPTS):
        assert await job_repository.claim("trabajador", ("osiptel",)) is not None
        await _expire_leases(pool)

    assert await job_repository.claim("trabajador", ("osiptel",)) is None
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


async def test_published_search_finds_a_dni_inside_a_ruc_and_paginates(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    """A RUC-10 embeds its owner's DNI, so a DNI search must return both rows."""
    job = await _submit_one(pool, service)
    reference = await object_reference(pool, job.team_id, "resultados/uno.json")
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

    found, more = await job_repository.search_published(
        job.team_id, "12345678", limit=20, offset=0
    )
    assert {result.document for result in found} == {"10123456789", "12345678"}
    assert more is False

    first_page, more = await job_repository.search_published(
        job.team_id, "12345678", limit=1, offset=0
    )
    assert len(first_page) == 1
    assert more is True


async def test_the_worker_api_leases_an_item_and_publishes_its_result(
    pool: asyncpg.Pool,
    service: PortalService,
    client: httpx.AsyncClient,
    protector: AesGcmSecretProtector,
) -> None:
    """The worker's only view of the queue is this pair of authenticated calls."""
    secret = await protector.protect({"username": "equipo", "password": "clave"})
    job = await _submit_one(pool, service, ciphertext=secret.ciphertext)
    headers = {
        "Authorization": f"Bearer {WORKER_TOKEN}",
        "X-Portal-Worker": "trabajador-uno",
    }
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
    service: PortalService,
    *,
    ciphertext: bytes = b"cifrado",
) -> Job:
    """Seed a team and admit a single running job holding one osiptel item."""
    actor_id, team_id, credential_id = await seed_team(pool, ciphertext=ciphertext)
    return await service.submit(
        SubmitJob(
            actor_id=actor_id,
            team_id=team_id,
            credential_version_id=credential_id,
            input_object_id=await object_reference(pool, team_id, "entradas/0.csv"),
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
