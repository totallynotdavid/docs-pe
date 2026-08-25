from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest

from portal.domain.errors import NotFound, PermissionDenied, Reason
from portal.domain.models import InputLine

from tests.portal.conftest import (
    object_reference,
    publish_claimed,
    seed_site_admin,
    seed_team,
    submit_command,
)


if TYPE_CHECKING:
    import asyncpg

    from portal.application.service import PortalService
    from portal.repository.jobs import PostgresJobRepository


async def create_input(pool: asyncpg.Pool, team_id: UUID) -> UUID:
    return await object_reference(pool, team_id, f"entradas/{uuid4().hex}.csv")


async def test_two_teams_confirming_the_same_document_share_one_entry_row(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    """Storage is deduplicated across every team: publish() upserts
    portal_entries on (document, source), so a second team's confirmation of
    a document another team already has updates the same row instead of
    creating a duplicate (see PostgresJobRepository.publish)."""
    team_a = await seed_team(pool)
    team_b = await seed_team(pool)

    await service.submit(
        submit_command(team_a, await create_input(pool, team_a.team_id))
    )
    await service.submit(
        submit_command(team_b, await create_input(pool, team_b.team_id))
    )

    claim_a = await job_repository.claim("trabajador", ("osiptel",))
    assert claim_a is not None
    assert await publish_claimed(pool, job_repository, claim_a)

    claim_b = await job_repository.claim("trabajador", ("osiptel",))
    assert claim_b is not None
    assert await publish_claimed(pool, job_repository, claim_b)

    entry_ids = await pool.fetch(
        "SELECT DISTINCT entry_id FROM portal_job_items WHERE document = '10412345678'"
    )
    entry_count = await pool.fetchval(
        "SELECT count(*) FROM portal_entries WHERE document = '10412345678'"
    )

    assert len(entry_ids) == 1
    assert entry_count == 1


async def test_team_search_never_surfaces_another_teams_entry(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    team_a = await seed_team(pool)
    team_b = await seed_team(pool)

    await service.submit(
        submit_command(team_a, await create_input(pool, team_a.team_id))
    )
    claimed = await job_repository.claim("trabajador", ("osiptel",))
    assert claimed is not None
    assert await publish_claimed(pool, job_repository, claimed)

    owner_results, _ = await service.search(
        team_a.actor_id, team_a.team_id, "10412345678", page=1
    )
    outsider_results, _ = await service.search(
        team_b.actor_id, team_b.team_id, "10412345678", page=1
    )

    assert len(owner_results) == 1
    assert outsider_results == ()


async def test_global_search_finds_an_entry_regardless_of_which_team_confirmed_it(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    team = await seed_team(pool)
    admin_id = await seed_site_admin(pool, "admin@osiptel.test")

    await service.submit(submit_command(team, await create_input(pool, team.team_id)))
    claimed = await job_repository.claim("trabajador", ("osiptel",))
    assert claimed is not None
    assert await publish_claimed(pool, job_repository, claimed)

    results, _ = await service.global_search(admin_id, "10412345678", page=1)

    assert len(results) == 1
    assert results[0].document == "10412345678"


async def test_reuse_offers_back_a_fresh_answer_but_not_a_stale_one(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    team = await seed_team(pool)

    await service.submit(submit_command(team, await create_input(pool, team.team_id)))
    claimed = await job_repository.claim("trabajador", ("osiptel",))
    assert claimed is not None
    assert await publish_claimed(pool, job_repository, claimed)

    lines = (InputLine(1, "10412345678"),)

    fresh_review = await service.preview_submission(
        team.actor_id, team.team_id, lines, ("osiptel",)
    )
    assert len(fresh_review.reusable) == 1

    # osiptel's reuse window is 7 days (see domain.planning.SOURCE_FRESHNESS).
    await pool.execute(
        "UPDATE portal_entries SET last_confirmed_at = now() - interval '8 days'"
        " WHERE document = '10412345678'"
    )

    stale_review = await service.preview_submission(
        team.actor_id, team.team_id, lines, ("osiptel",)
    )
    assert stale_review.reusable == ()


async def test_a_failed_entry_is_never_offered_as_reusable(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    team = await seed_team(pool)

    await service.submit(submit_command(team, await create_input(pool, team.team_id)))
    claimed = await job_repository.claim("trabajador", ("osiptel",))
    assert claimed is not None
    assert await publish_claimed(
        pool, job_repository, claimed, status="failed", rows=()
    )

    review = await service.preview_submission(
        team.actor_id,
        team.team_id,
        (InputLine(1, "10412345678"),),
        ("osiptel",),
    )

    assert review.reusable == ()


async def test_reuse_eligibility_never_crosses_the_team_boundary(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    """The shared portal_entries row exists the moment either team confirms
    it, but reusable_for_team scopes strictly through the caller's own
    portal_job_items -- a document team_b never itself confirmed must never
    be handed back as "already known", even though team_a's answer is
    sitting right there in the same deduplicated row."""
    team_a = await seed_team(pool)
    team_b = await seed_team(pool)

    await service.submit(
        submit_command(team_a, await create_input(pool, team_a.team_id))
    )
    claimed = await job_repository.claim("trabajador", ("osiptel",))
    assert claimed is not None
    assert await publish_claimed(pool, job_repository, claimed)

    review = await service.preview_submission(
        team_b.actor_id,
        team_b.team_id,
        (InputLine(1, "10412345678"),),
        ("osiptel",),
    )

    assert review.reusable == ()


async def test_global_search_is_gated_by_site_admin_or_team_entitlement(
    pool: asyncpg.Pool,
    service: PortalService,
) -> None:
    team = await seed_team(pool)
    admin_id = await seed_site_admin(pool, "admin@osiptel.test")

    with pytest.raises(PermissionDenied) as raised:
        await service.global_search(team.actor_id, "104", page=1)
    assert raised.value.reason is Reason.GLOBAL_SEARCH_REQUIRED

    await service.set_global_search(admin_id, team.team_id, enabled=True)

    # No longer denied, whether or not anything matches.
    await service.global_search(team.actor_id, "104", page=1)


async def test_entry_lookup_is_team_scoped_but_global_entry_requires_access(
    pool: asyncpg.Pool,
    service: PortalService,
    job_repository: PostgresJobRepository,
) -> None:
    team_a = await seed_team(pool)
    team_b = await seed_team(pool)
    admin_id = await seed_site_admin(pool, "admin@osiptel.test")

    await service.submit(
        submit_command(team_a, await create_input(pool, team_a.team_id))
    )
    claimed = await job_repository.claim("trabajador", ("osiptel",))
    assert claimed is not None
    assert await publish_claimed(pool, job_repository, claimed)

    job = await service.job(team_a.actor_id, team_a.team_id, claimed.job_id)
    entry_id = job.items[0].entry_id
    assert entry_id is not None

    found = await service.entry(team_a.actor_id, team_a.team_id, entry_id)
    assert found.document == "10412345678"

    with pytest.raises(NotFound) as not_found:
        await service.entry(team_b.actor_id, team_b.team_id, entry_id)
    assert not_found.value.reason is Reason.ENTRY_NOT_FOUND

    with pytest.raises(PermissionDenied) as denied:
        await service.global_entry(team_b.actor_id, entry_id)
    assert denied.value.reason is Reason.GLOBAL_SEARCH_REQUIRED

    via_admin = await service.global_entry(admin_id, entry_id)
    assert via_admin.document == "10412345678"
