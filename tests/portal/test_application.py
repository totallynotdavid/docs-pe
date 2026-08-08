from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from portal.domain.errors import (
    CredentialConfigurationError,
    NotFound,
    PermissionDenied,
    Reason,
    SourceValidationError,
)
from portal.domain.models import DeliveryChannel, JobState, TeamRole

from tests.portal.conftest import (
    object_reference,
    seed_site_admin,
    seed_team,
    seed_user,
    submit_command,
)


if TYPE_CHECKING:
    import asyncpg

    from portal.application.service import PortalService
    from portal.repository.jobs import PostgresJobRepository
    from portal.repository.teams import PostgresTeamRepository


async def create_input(pool: asyncpg.Pool, team_id: UUID) -> UUID:
    return await object_reference(pool, team_id, f"entradas/{uuid4().hex}.csv")


async def test_team_member_cannot_submit_a_process(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    service: PortalService,
) -> None:
    team = await seed_team(pool)
    member_id = await seed_user(pool)
    await team_repository.add_member(team.team_id, member_id, TeamRole.TEAM_MEMBER)

    command = submit_command(
        team,
        await create_input(pool, team.team_id),
        actor_id=member_id,
    )

    with pytest.raises(PermissionDenied) as raised:
        await service.submit(command)

    assert raised.value.reason is Reason.LEADER_REQUIRED
    assert await pool.fetchval("SELECT count(*) FROM portal_jobs") == 0


async def test_credential_cannot_cross_team_boundary(
    pool: asyncpg.Pool,
    service: PortalService,
) -> None:
    team_a = await seed_team(pool)
    team_b = await seed_team(pool)

    command = submit_command(
        team_a._replace(credential_id=team_b.credential_id),
        await create_input(pool, team_a.team_id),
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

    await team_repository.add_member(
        team_a.team_id,
        member_a,
        TeamRole.TEAM_MEMBER,
    )

    job_a = await service.submit(
        submit_command(team_a, await create_input(pool, team_a.team_id))
    )
    job_b = await service.submit(
        submit_command(team_b, await create_input(pool, team_b.team_id))
    )

    for job in (job_a, job_b):
        claimed = await job_repository.claim("trabajador", ("osiptel",))
        assert claimed is not None

        published = await job_repository.publish(
            claimed.item_id,
            "trabajador",
            claimed.lease_fence,
            await object_reference(pool, job.team_id, f"salida/{job.id}.json"),
        )
        assert published

    results = await service.published_results(member_a, team_a.team_id)

    assert [job.id for job in results] == [job_a.id]


async def test_only_stable_fetch_sources_are_allowed(
    pool: asyncpg.Pool,
    service: PortalService,
) -> None:
    team = await seed_team(pool)

    command = submit_command(
        team,
        await create_input(pool, team.team_id),
        sources=("portabilidad",),
    )

    with pytest.raises(SourceValidationError) as raised:
        await service.submit(command)

    assert raised.value.reason is Reason.SOURCE_NOT_ENABLED
    assert raised.value.params["invalid"] == "portabilidad"


async def test_all_excluded_input_is_terminal_and_creates_outbox_intents(
    pool: asyncpg.Pool,
    service: PortalService,
) -> None:
    team = await seed_team(pool)

    job = await service.submit(
        submit_command(
            team,
            await create_input(pool, team.team_id),
            value="inválido",
        )
    )

    assert job.state is JobState.COMPLETED
    assert job.items == []

    stored = await service.job(team.actor_id, team.team_id, job.id)

    assert [excluded.reason for excluded in stored.exclusions] == ["invalid_document"]

    events = await pool.fetch(
        "SELECT event_type FROM portal_job_events WHERE job_id = $1",
        job.id,
    )

    assert [row["event_type"] for row in events] == ["job.completed"]

    rows = await pool.fetch(
        """
        SELECT DISTINCT outbox.channel
          FROM portal_notification_outbox AS outbox
          JOIN portal_job_events AS event ON event.id = outbox.event_id
         WHERE event.job_id = $1
        """,
        job.id,
    )

    assert {row["channel"] for row in rows} == {
        channel.value for channel in DeliveryChannel
    }


async def test_renaming_a_credential_updates_its_label(
    pool: asyncpg.Pool,
    service: PortalService,
) -> None:
    team = await seed_team(pool)
    [credential] = await service.credentials(team.actor_id, team.team_id)

    await service.rename_credential(
        team.actor_id,
        team.team_id,
        credential.credential_id,
        "Nueva etiqueta",
    )

    [renamed] = await service.credentials(team.actor_id, team.team_id)
    assert renamed.label == "Nueva etiqueta"


async def test_renaming_a_credential_cannot_collide_with_another_in_the_team(
    pool: asyncpg.Pool,
    service: PortalService,
) -> None:
    team = await seed_team(pool)
    [first] = await service.credentials(team.actor_id, team.team_id)

    await pool.execute(
        """
        INSERT INTO portal_team_proxy_credentials (id, team_id, label, created_by)
        VALUES ($1, $2, 'Otra conexión', $3)
        """,
        uuid4(),
        team.team_id,
        team.actor_id,
    )

    with pytest.raises(CredentialConfigurationError) as raised:
        await service.rename_credential(
            team.actor_id,
            team.team_id,
            first.credential_id,
            "Otra conexión",
        )

    assert raised.value.reason is Reason.LABEL_TAKEN


async def test_renaming_a_credential_cannot_cross_team_boundary(
    pool: asyncpg.Pool,
    service: PortalService,
) -> None:
    team_a = await seed_team(pool)
    team_b = await seed_team(pool)
    [credential_b] = await service.credentials(team_b.actor_id, team_b.team_id)

    with pytest.raises(NotFound) as raised:
        await service.rename_credential(
            team_a.actor_id,
            team_a.team_id,
            credential_b.credential_id,
            "Robado",
        )

    assert raised.value.reason is Reason.CREDENTIAL_WRONG_TEAM


async def test_searching_logs_the_query_and_result_count_for_the_team(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    job_repository: PostgresJobRepository,
    service: PortalService,
) -> None:
    team = await seed_team(pool)
    member_id = await seed_user(pool)
    await team_repository.add_member(team.team_id, member_id, TeamRole.TEAM_MEMBER)

    job = await service.submit(
        submit_command(team, await create_input(pool, team.team_id))
    )
    claimed = await job_repository.claim("trabajador", ("osiptel",))
    assert claimed is not None
    await job_repository.publish(
        claimed.item_id,
        "trabajador",
        claimed.lease_fence,
        await object_reference(pool, team.team_id, f"salida/{job.id}.json"),
    )

    results, _ = await service.search(member_id, team.team_id, "10412345678", page=1)
    assert len(results) == 1

    with pytest.raises(PermissionDenied) as raised:
        await service.recent_searches(member_id, team.team_id)
    assert raised.value.reason is Reason.LEADER_REQUIRED

    entries = await service.recent_searches(team.actor_id, team.team_id)
    assert len(entries) == 1
    assert entries[0].query == "10412345678"
    assert entries[0].result_count == 1


async def test_admin_search_activity_shows_team_counts_not_query_text(
    pool: asyncpg.Pool,
    service: PortalService,
) -> None:
    team_a = await seed_team(pool)
    team_b = await seed_team(pool)
    admin_id = await seed_site_admin(pool, "admin@osiptel.test")

    await service.search(team_a.actor_id, team_a.team_id, "12345678", page=1)
    await service.search(team_a.actor_id, team_a.team_id, "87654321", page=1)

    activity = await service.team_search_activity(admin_id)
    by_team = {row.team_id: row.search_count for row in activity}

    assert by_team[team_a.team_id] == 2
    assert by_team[team_b.team_id] == 0


async def test_a_site_admins_activity_feed_covers_every_team_not_just_their_own(
    pool: asyncpg.Pool,
    service: PortalService,
) -> None:
    team = await seed_team(pool)
    admin_id = await seed_site_admin(pool, "admin@osiptel.test")

    job = await service.submit(
        submit_command(
            team,
            await create_input(pool, team.team_id),
            value="inválido",
        )
    )
    assert job.state is JobState.COMPLETED

    events = await service.notifications(admin_id)

    assert any(event.job_id == job.id for event in events)
