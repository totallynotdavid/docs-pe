from __future__ import annotations

import asyncio
import contextlib

from typing import TYPE_CHECKING

import pytest

from core.proxy import registry as proxy_registry
from core.proxy.base import ProviderSpec, ProviderTuning
from core.sites import registry as site_registry
from portal.credentials.secrets import encode_config
from portal.domain.models import ItemState, JobState
from portal.worker.agent import AgentOptions, WorkerAgent

from tests.conftest import FakeProvider, as_async, fake_site
from tests.portal.conftest import (
    WORKER_ID,
    enroll_worker,
    object_reference,
    seed_team,
    submit_command,
)


if TYPE_CHECKING:
    import asyncpg

    from litestar.testing import AsyncTestClient
    from portal.application.service import PortalService
    from portal.credentials.secrets import EnvelopeProtector
    from portal.repository.jobs import PostgresJobRepository
    from portal.settings import PortalSettings


PUBLISH_TIMEOUT_SECONDS = 10.0
POLL_INTERVAL_SECONDS = 0.05


@pytest.fixture(autouse=True)
def _stub_the_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the only two things a real fetch reaches outside this
    process: the target site's HTTP response and the proxy provider's own
    session. Claiming, credential reveal, execution's retry/breaker
    plumbing, publishing, and the S3 write all still run for real."""
    monkeypatch.setitem(
        site_registry.SITES,
        "osiptel",
        fake_site(
            "osiptel",
            "documento",
            lookup=as_async(lambda client, doc: ((str(doc),),)),
        ),
    )
    monkeypatch.setitem(
        proxy_registry.PROVIDERS,
        "geonode",
        ProviderSpec(
            name="geonode",
            fields=(),
            tuning=ProviderTuning(workers=1, ban_cooldown_s=0.0),
            normalize=lambda raw: dict(raw),
            build=lambda raw: FakeProvider(),
        ),
    )


async def test_worker_agent_claims_executes_and_publishes_the_happy_path(
    pool: asyncpg.Pool,
    settings: PortalSettings,
    protector: EnvelopeProtector,
    service: PortalService,
    job_repository: PostgresJobRepository,
    worker_client: AsyncTestClient,
) -> None:
    """A worker claims, executes, and publishes a queued item."""
    team = await seed_team(
        pool,
        config=protector.protect(encode_config({"password": "no-importa"})),
    )

    job = await service.submit(
        submit_command(
            team,
            await object_reference(pool, team.team_id, "entradas/1.csv"),
            value="10412345678",
            sources=("osiptel",),
        )
    )
    assert job.state is JobState.RUNNING

    worker_client.headers.update(await enroll_worker(pool))

    agent = WorkerAgent(
        AgentOptions(
            worker_api_url="https://worker-api.tailnet.test",
            credential="unused-the-client-fixture-already-carries-real-auth",
            database_dsn=settings.database_dsn,
            worker_id=WORKER_ID,
            sources=("osiptel",),
            concurrency=1,
        )
    )

    run_task = asyncio.create_task(agent.run_with(pool, worker_client))
    try:
        deadline = asyncio.get_running_loop().time() + PUBLISH_TIMEOUT_SECONDS
        finished = await job_repository.job(job.id, team.team_id)

        while finished is not None and finished.state is not JobState.COMPLETED:
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("worker agent did not publish the claimed item in time")

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            finished = await job_repository.job(job.id, team.team_id)
    finally:
        run_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await run_task

    assert finished is not None
    assert finished.state is JobState.COMPLETED
    assert len(finished.items) == 1
    assert finished.items[0].state is ItemState.PUBLISHED
    assert finished.items[0].entry_id is not None
