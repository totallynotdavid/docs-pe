from __future__ import annotations

from uuid import uuid4

import pytest

from portal.application.service import PortalService
from portal.domain.errors import PermissionDenied, SourceValidationError
from portal.domain.models import DeliveryChannel, JobState, TeamRole
from portal.repository.memory import InMemoryPortalRepository
from portal.testing import command, leader


async def test_team_member_cannot_submit_a_process(
    repository: InMemoryPortalRepository, service: PortalService
) -> None:
    _, team_id, credential_id = leader(repository)
    member_id = uuid4()
    repository.grant(member_id, team_id, TeamRole.TEAM_MEMBER)

    with pytest.raises(PermissionDenied, match="líder"):
        await service.submit(command(member_id, team_id, credential_id))

    assert repository.jobs == {}


async def test_credential_cannot_cross_team_boundary(
    repository: InMemoryPortalRepository, service: PortalService
) -> None:
    leader_a, team_a, _credential_a = leader(repository)
    _leader_b, _team_b, credential_b = leader(repository)

    with pytest.raises(PermissionDenied, match="mismo equipo"):
        await service.submit(command(leader_a, team_a, credential_b))


async def test_members_only_search_their_team_published_results(
    repository: InMemoryPortalRepository, service: PortalService
) -> None:
    leader_a, team_a, credential_a = leader(repository)
    leader_b, team_b, credential_b = leader(repository)
    member_a = uuid4()
    repository.grant(member_a, team_a, TeamRole.TEAM_MEMBER)
    job_a = await service.submit(command(leader_a, team_a, credential_a))
    job_b = await service.submit(command(leader_b, team_b, credential_b))
    assert await repository.record_published_result(
        job_a.id, job_a.items[0].id, job_a.lease_fence, uuid4()
    )
    assert await repository.record_published_result(
        job_b.id, job_b.items[0].id, job_b.lease_fence, uuid4()
    )

    assert [job.id for job in await service.published_results(member_a, team_a)] == [
        job_a.id
    ]


async def test_only_stable_fetch_sources_are_allowed(
    repository: InMemoryPortalRepository, service: PortalService
) -> None:
    leader_id, team_id, credential_id = leader(repository)

    with pytest.raises(SourceValidationError, match="browser"):
        await service.submit(
            command(leader_id, team_id, credential_id, sources=("browser",))
        )


async def test_all_excluded_input_is_terminal_and_creates_outbox_intents(
    repository: InMemoryPortalRepository, service: PortalService
) -> None:
    leader_id, team_id, credential_id = leader(repository)
    job = await service.submit(
        command(leader_id, team_id, credential_id, value="inválido")
    )

    assert job.state is JobState.COMPLETED
    assert job.items == []
    assert len(job.exclusions) == 1
    assert [event.event_type for event in repository.events] == ["proceso.completed"]
    assert {intent.channel for intent in repository.outbox} == set(DeliveryChannel)


async def test_cancellation_fences_late_work_and_preserves_published_result(
    repository: InMemoryPortalRepository, service: PortalService
) -> None:
    leader_id, team_id, credential_id = leader(repository)
    job = await service.submit(command(leader_id, team_id, credential_id))
    item = job.items[0]
    published_object_id = uuid4()
    assert await repository.record_published_result(
        job.id, item.id, job.lease_fence, published_object_id
    )

    cancelled = await service.cancel(leader_id, team_id, job.id)

    assert cancelled.state is JobState.CANCELLED
    assert item.result_object_id == published_object_id
    assert (
        await repository.record_published_result(job.id, item.id, 0, uuid4()) is False
    )
    assert {intent.channel for intent in repository.outbox} == set(DeliveryChannel)
    assert repository.events[-1].event_type == "proceso.cancelled"
