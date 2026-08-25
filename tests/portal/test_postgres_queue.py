from __future__ import annotations

import asyncio
import base64

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from portal.credentials.secrets import encode_config
from portal.domain.models import (
    MAX_LEASE_ATTEMPTS,
    InputLine,
    Job,
    JobState,
    ProtectedSecret,
    SubmitJob,
)
from portal.repository.workers import PostgresWorkerRegistry

from tests.portal.conftest import (
    UNREADABLE_SECRET,
    WORKER_ID,
    enroll_worker,
    object_reference,
    seed_site_admin,
    seed_team,
    submit_command,
)


if TYPE_CHECKING:
    import asyncpg

    from litestar.testing import AsyncTestClient
    from portal.application.service import PortalService
    from portal.credentials.secrets import EnvelopeProtector
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
    actor_id, team_id, credential_id = await seed_team(pool)

    commands = [
        SubmitJob(
            actor_id=actor_id,
            team_id=team_id,
            credential_version_id=credential_id,
            input_object_id=await object_reference(
                pool,
                team_id,
                f"entradas/{number}.csv",
            ),
            filename=f"entrada-{number}.csv",
            sources=("osiptel",),
            lines=(InputLine(1, "10412345678"),),
        )
        for number in range(10)
    ]

    jobs = await asyncio.gather(*(service.submit(command) for command in commands))
    running_jobs = [job for job in jobs if job.state is JobState.RUNNING]
    queued_jobs = sorted(
        (job for job in jobs if job.state is JobState.QUEUED),
        key=lambda job: job.queue_sequence,
    )

    assert len(running_jobs) == 5
    assert [job.queue_sequence for job in queued_jobs] == [6, 7, 8, 9, 10]

    claimed = await job_repository.claim("trabajador-prueba", ("osiptel",))

    assert claimed is not None

    partial_job = next(job for job in running_jobs if job.id != claimed.job_id)
    result_reference = await object_reference(
        pool,
        team_id,
        "resultados/uno.json",
    )

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

    assert queued_jobs[0].id == await pool.fetchval(
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

    partial_cancelled = await service.cancel(
        actor_id,
        team_id,
        partial_job.id,
    )

    assert partial_cancelled.state is JobState.CANCELLED
    assert await pool.fetchval("SELECT count(*) FROM portal_notification_outbox") == 6


async def test_an_expired_lease_returns_its_item_to_the_queue(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    await _submit_one(pool, service)

    first_claim = await job_repository.claim(
        "trabajador-uno",
        ("osiptel",),
    )

    assert first_claim is not None
    assert await _attempts(pool, first_claim.item_id) == 1

    await _expire_leases(pool)

    second_claim = await job_repository.claim(
        "trabajador-dos",
        ("osiptel",),
    )

    assert second_claim is not None
    assert second_claim.item_id == first_claim.item_id
    assert await _attempts(pool, second_claim.item_id) == 2


async def test_a_repeatedly_expired_item_retires_and_fails_its_job(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    job = await _submit_one(pool, service)

    for _ in range(MAX_LEASE_ATTEMPTS):
        claimed = await job_repository.claim(
            "trabajador",
            ("osiptel",),
        )

        assert claimed is not None

        await _expire_leases(pool)

    assert (
        await job_repository.claim(
            "trabajador",
            ("osiptel",),
        )
        is None
    )

    item = await pool.fetchrow(
        "SELECT state, reason FROM portal_job_items WHERE job_id = $1",
        job.id,
    )

    assert item["state"] == "failed"
    assert item["reason"] == "lease_expired"

    finished = await pool.fetchrow(
        "SELECT state, terminal_reason FROM portal_jobs WHERE id = $1",
        job.id,
    )

    assert finished["state"] == "failed"
    assert finished["terminal_reason"] == "no_results"


async def test_claim_prefers_a_lane_s_held_session_over_plain_fifo(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    """A lane already holding a (source, credential) session should keep
    draining that pair's work, so its session gets reused across claims
    instead of being closed after one lookup and reopened for the next."""
    team_earlier = await seed_team(pool)
    team_later = await seed_team(pool)

    earlier_input = await object_reference(pool, team_earlier.team_id, "e.csv")
    later_input = await object_reference(pool, team_later.team_id, "l.csv")

    earlier_job = await service.submit(submit_command(team_earlier, earlier_input))
    later_job = await service.submit(submit_command(team_later, later_input))

    assert earlier_job.queue_sequence < later_job.queue_sequence

    claimed = await job_repository.claim(
        "trabajador",
        ("osiptel",),
        affinity_source="osiptel",
        affinity_credential_version_id=team_later.credential_id,
    )

    assert claimed is not None
    assert claimed.job_id == later_job.id
    assert claimed.credential_version_id == team_later.credential_id


async def test_claim_falls_back_to_fifo_without_a_matching_affinity(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    team_earlier = await seed_team(pool)
    team_later = await seed_team(pool)

    earlier_input = await object_reference(pool, team_earlier.team_id, "e.csv")
    later_input = await object_reference(pool, team_later.team_id, "l.csv")

    earlier_job = await service.submit(submit_command(team_earlier, earlier_input))
    await service.submit(submit_command(team_later, later_input))

    claimed = await job_repository.claim("trabajador", ("osiptel",))

    assert claimed is not None
    assert claimed.job_id == earlier_job.id


async def test_claim_skips_a_pair_whose_circuit_breaker_is_open(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    job = await _submit_one(pool, service)

    await pool.execute(
        """
        INSERT INTO portal_circuit_breakers
            (source, provider, consecutive_failures, level, open_until)
        VALUES ('osiptel', 'geonode', 0, 1, now() + interval '1 hour')
        """
    )

    assert await job_repository.claim("trabajador", ("osiptel",)) is None

    await pool.execute(
        "UPDATE portal_circuit_breakers SET open_until = now() - interval '1 second'"
    )

    claimed = await job_repository.claim("trabajador", ("osiptel",))

    assert claimed is not None
    assert claimed.job_id == job.id


async def test_published_search_finds_a_dni_inside_a_ruc_and_paginates(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    job = await _submit_one(pool, service)
    reference = await object_reference(
        pool,
        job.team_id,
        "resultados/uno.json",
    )

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
        job.team_id,
        "12345678",
        limit=20,
        offset=0,
    )

    assert {result.document for result in found} == {
        "10123456789",
        "12345678",
    }
    assert more is False

    first_page, more = await job_repository.search_published(
        job.team_id,
        "12345678",
        limit=1,
        offset=0,
    )

    assert len(first_page) == 1
    assert more is True


async def test_the_worker_api_leases_an_item_and_publishes_its_result(
    pool: asyncpg.Pool,
    service: PortalService,
    worker_client: AsyncTestClient,
    protector: EnvelopeProtector,
) -> None:
    config = protector.protect(
        encode_config({"username": "equipo", "password": "clave"})
    )

    job = await _submit_one(pool, service, config=config)
    headers = await enroll_worker(pool)

    anonymous = await worker_client.post("/claim", json={"sources": []})

    assert anonymous.status_code == 403

    response = await worker_client.post(
        "/claim",
        json={"sources": ["osiptel"]},
        headers=headers,
    )

    assert response.status_code == 200

    claimed = response.json()

    assert claimed["document"] == "10412345678"
    assert claimed["credential_version_id"] == str(job.credential_version_id)
    assert claimed["credential"]["config"] == {
        "username": "equipo",
        "password": "clave",
    }

    published = await worker_client.post(
        "/publish",
        json={
            "item_id": claimed["item_id"],
            "fence": claimed["fence"],
            "source": claimed["source"],
            "provider": claimed["credential"]["provider"],
            "healthy_contact": True,
            "content": base64.b64encode(b'{"lineas": []}').decode("ascii"),
        },
        headers=headers,
    )

    assert published.status_code == 200
    assert published.json() == {"published": True}

    finished = await pool.fetchrow(
        "SELECT state FROM portal_jobs WHERE id = $1",
        job.id,
    )

    assert finished["state"] == "completed"

    breaker = await pool.fetchrow(
        "SELECT consecutive_failures, level, open_until "
        "FROM portal_circuit_breakers WHERE source = $1 AND provider = $2",
        claimed["source"],
        claimed["credential"]["provider"],
    )

    assert breaker is not None
    assert breaker["consecutive_failures"] == 0
    assert breaker["level"] == 0
    assert breaker["open_until"] is None


async def test_a_heartbeat_updates_status_and_staleness_flips_it_offline(
    pool: asyncpg.Pool,
    worker_client: AsyncTestClient,
    service: PortalService,
) -> None:
    headers = await enroll_worker(pool)

    beat = await worker_client.post(
        "/heartbeat",
        json={"cpu_percent": 12.5, "memory_mb": 256.0, "current_job_id": None},
        headers=headers,
    )
    assert beat.status_code == 204

    admin_id = await seed_site_admin(pool, "admin@osiptel.test")
    health = await service.system_health(admin_id)

    assert len(health.workers) == 1
    worker = health.workers[0]
    assert worker.worker_id == WORKER_ID
    assert worker.online is True
    assert worker.cpu_percent == pytest.approx(12.5)
    assert worker.memory_mb == pytest.approx(256.0)

    # Backdate the heartbeat past the staleness window to prove "offline" is
    # computed from recency, not just from whether one was ever recorded.
    await pool.execute(
        """
        UPDATE portal_workers
           SET last_seen_at = now() - interval '1 hour'
         WHERE worker_id = $1
        """,
        WORKER_ID,
    )

    stale = await service.system_health(admin_id)
    assert stale.workers[0].online is False


async def test_a_revoked_worker_stops_claiming(
    pool: asyncpg.Pool,
    service: PortalService,
    worker_client: AsyncTestClient,
) -> None:
    await _submit_one(pool, service)
    headers = await enroll_worker(pool)

    await PostgresWorkerRegistry(pool).revoke(WORKER_ID)

    refused = await worker_client.post(
        "/claim",
        json={"sources": ["osiptel"]},
        headers=headers,
    )

    assert refused.status_code == 403
    assert refused.json()["reason"] == "worker_not_authorized"


async def _submit_one(
    pool: asyncpg.Pool,
    service: PortalService,
    *,
    config: ProtectedSecret = UNREADABLE_SECRET,
) -> Job:
    actor_id, team_id, credential_id = await seed_team(pool, config=config)

    return await service.submit(
        SubmitJob(
            actor_id=actor_id,
            team_id=team_id,
            credential_version_id=credential_id,
            input_object_id=await object_reference(
                pool,
                team_id,
                "entradas/0.csv",
            ),
            filename="entrada.csv",
            sources=("osiptel",),
            lines=(InputLine(1, "10412345678"),),
        )
    )


async def _attempts(pool: asyncpg.Pool, item_id: UUID) -> int:
    return int(
        await pool.fetchval(
            "SELECT attempts FROM portal_job_items WHERE id = $1",
            item_id,
        )
    )
