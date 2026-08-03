"""Team authorization and submission planning, against the real repository."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from portal.domain.errors import PermissionDenied, Reason, SourceValidationError
from portal.domain.models import DeliveryChannel, JobState, TeamRole

from tests.portal.conftest import object_reference, seed_team, seed_user, submit_command


if TYPE_CHECKING:
    import asyncpg

    from portal.application.service import PortalService
    from portal.repository.jobs import PostgresJobRepository
    from portal.repository.teams import PostgresTeamRepository


async def _input(pool: asyncpg.Pool, team_id: UUID) -> UUID:
    return await object_reference(pool, team_id, f"entradas/{uuid4().hex}.csv")


async def test_team_member_cannot_submit_a_process(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    service: PortalService,
) -> None:
    team = await seed_team(pool)
    member_id = await seed_user(pool)
    await team_repository.add_member(team.team_id, member_id, TeamRole.TEAM_MEMBER)
    command = submit_command(team, await _input(pool, team.team_id), actor_id=member_id)

    with pytest.raises(PermissionDenied) as raised:
        await service.submit(command)

    assert raised.value.reason is Reason.LEADER_REQUIRED
    assert await pool.fetchval("SELECT count(*) FROM portal_jobs") == 0


async def test_credential_cannot_cross_team_boundary(
    pool: asyncpg.Pool, service: PortalService
) -> None:
    team_a = await seed_team(pool)
    team_b = await seed_team(pool)
    command = submit_command(
        team_a._replace(credential_id=team_b.credential_id),
        await _input(pool, team_a.team_id),
    )

    with pytest.raises(PermissionDenied) as raised:
        await service.submit(command)

    assert raised.value.reason is Reason.CREDENTIAL_WRONG_TEAM


async def test_members_only_search_their_team_published_results(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    service: PortalService,
) -> None:
    team_a = await seed_team(pool)
    team_b = await seed_team(pool)
    member_a = await seed_user(pool)
    await team_repository.add_member(team_a.team_id, member_a, TeamRole.TEAM_MEMBER)
    job_a = await service.submit(
        submit_command(team_a, await _input(pool, team_a.team_id))
    )
    job_b = await service.submit(
        submit_command(team_b, await _input(pool, team_b.team_id))
    )
    for job in (job_a, job_b):
        claimed = await job_repository.claim("trabajador", ("osiptel",))
        assert claimed is not None
        assert await job_repository.publish(
            claimed.item_id,
            "trabajador",
            claimed.lease_fence,
            await object_reference(pool, job.team_id, f"salida/{job.id}.json"),
        )

    published = await service.published_results(member_a, team_a.team_id)

    assert [job.id for job in published] == [job_a.id]
    assert job_b.id not in {job.id for job in published}


async def test_only_stable_fetch_sources_are_allowed(
    pool: asyncpg.Pool, service: PortalService
) -> None:
    team = await seed_team(pool)
    command = submit_command(
        team, await _input(pool, team.team_id), sources=("portabilidad",)
    )

    with pytest.raises(SourceValidationError) as raised:
        await service.submit(command)

    assert raised.value.reason is Reason.SOURCE_NOT_ENABLED
    assert raised.value.params["invalid"] == "portabilidad"


async def test_all_excluded_input_is_terminal_and_creates_outbox_intents(
    pool: asyncpg.Pool, service: PortalService
) -> None:
    team = await seed_team(pool)

    job = await service.submit(
        submit_command(team, await _input(pool, team.team_id), value="inválido")
    )

    assert job.state is JobState.COMPLETED
    assert job.items == []
    # admit_submission returns the admitted work; the exclusions are persisted and
    # read back by `job`, which is what the detail page renders.
    stored = await service.job(team.actor_id, team.team_id, job.id)
    assert [excluded.reason for excluded in stored.exclusions] == ["documento_invalido"]
    events = await pool.fetch(
        "SELECT event_type FROM portal_job_events WHERE job_id = $1", job.id
    )
    assert [row["event_type"] for row in events] == ["proceso.completed"]
    channels = await pool.fetch(
        """
        SELECT DISTINCT outbox.channel
          FROM portal_notification_outbox AS outbox
          JOIN portal_job_events AS event ON event.id = outbox.event_id
         WHERE event.job_id = $1
        """,
        job.id,
    )
    assert {row["channel"] for row in channels} == {
        channel.value for channel in DeliveryChannel
    }
