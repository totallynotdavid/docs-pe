from __future__ import annotations

import asyncio

from portal.application.service import PortalService
from portal.domain.models import ACTIVE_JOB_STATES, JobState
from portal.repository.memory import InMemoryPortalRepository
from portal.testing import command, leader


async def test_five_process_admission_is_exact_under_concurrent_submissions(
    repository: InMemoryPortalRepository, service: PortalService
) -> None:
    leader_id, team_id, credential_id = leader(repository)
    jobs = await asyncio.gather(
        *(service.submit(command(leader_id, team_id, credential_id)) for _ in range(10))
    )

    running = [job for job in jobs if job.state is JobState.RUNNING]
    queued = sorted(
        (job for job in jobs if job.state is JobState.QUEUED),
        key=lambda job: job.queue_sequence,
    )
    assert len(running) == 5
    assert [job.queue_sequence for job in queued] == [6, 7, 8, 9, 10]

    await repository.complete(running[0].id)

    assert queued[0].state is JobState.RUNNING
    assert sum(job.state in ACTIVE_JOB_STATES for job in repository.jobs.values()) == 5


async def test_fifo_promotion_never_skips_the_oldest_queued_process(
    repository: InMemoryPortalRepository, service: PortalService
) -> None:
    leader_id, team_id, credential_id = leader(repository)
    jobs = [
        await service.submit(command(leader_id, team_id, credential_id))
        for _ in range(7)
    ]
    oldest_queued, newest_queued = jobs[5:]

    await repository.complete(jobs[1].id)

    assert oldest_queued.state is JobState.RUNNING
    assert newest_queued.state is JobState.QUEUED
