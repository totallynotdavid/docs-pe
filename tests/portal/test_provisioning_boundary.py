from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import asyncpg
import pyotp
import pytest

from portal.domain.errors import NotFound, ProvisioningError, Reason
from portal.domain.models import RequestTrace, TeamRole
from portal.security import RECOVERY_CODE_COUNT, hash_password

from tests.portal.conftest import (
    ORIGIN,
    PASSWORD,
    csrf_token,
    login,
    seed_site_admin,
    seed_team,
    seed_user,
    sync_client,
)


if TYPE_CHECKING:
    from litestar import Litestar
    from portal.application.provisioning import ProvisioningService
    from portal.repository.teams import PostgresTeamRepository
    from portal.settings import PortalSettings


async def _seed_member(pool: asyncpg.Pool, email: str) -> str:
    user_id = await seed_user(pool, email=email)

    await pool.execute(
        "UPDATE portal_users SET password_hash = $2 WHERE id = $1",
        user_id,
        hash_password(PASSWORD),
    )

    return email


async def test_first_team_setup_is_the_only_empty_installation_path(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    admin_email = "admin@osiptel.test"
    admin_id = await seed_site_admin(pool, admin_email)

    with sync_client(app) as client:
        assert login(client, admin_email).status_code == 303

        response = client.get("/", follow_redirects=False)
        assert response.headers["location"] == "/setup"

        page = client.get("/setup")
        assert "UUID" not in page.text
        assert "persona líder" not in page.text

        response = client.post(
            "/setup",
            data={
                "name": "Equipo Lima",
                "slug": "equipo-lima",
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert response.status_code == 303

        location = response.headers["location"]
        assert location.endswith("/settings/proxy")
        assert "config_ciphertext" not in client.get(location).text

    team = await team_repository.team_by_slug("equipo-lima")

    assert team is not None
    assert await team_repository.role_for(admin_id, team.id) is TeamRole.TEAM_LEADER


async def test_site_and_team_settings_use_email_selectors_and_keep_members_limited(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    admin_email = "admin@osiptel.test"

    await seed_site_admin(pool, admin_email)
    leader_email = await _seed_member(pool, "lider@osiptel.test")
    member_email = await _seed_member(pool, "miembro@osiptel.test")

    admin_id = await pool.fetchval(
        "SELECT id FROM portal_users WHERE email = $1",
        admin_email,
    )
    await team_repository.create_first_team("inicial", "Inicial", admin_id)

    with sync_client(app) as admin_client:
        assert login(admin_client, admin_email).status_code == 303

        page = admin_client.get("/admin/teams")
        assert "leader_id" not in page.text
        assert "ID de la persona" not in page.text

        response = admin_client.post(
            "/admin/teams",
            data={
                "name": "Consultas Norte",
                "slug": "consultas-norte",
                "leader_email": leader_email,
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert response.status_code == 303

        team_url = response.headers["location"].removesuffix("/settings")

        response = admin_client.post(
            "/admin/members",
            headers={"Origin": ORIGIN},
        )
        assert response.status_code == 404

    with sync_client(app) as leader_client:
        assert login(leader_client, leader_email).status_code == 303

        page = leader_client.get(f"{team_url}/settings/members")

        response = leader_client.post(
            f"{team_url}/settings/members",
            data={
                "email": member_email,
                "role": "team_member",
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert leader_client.get(f"{team_url}/credentials").status_code == 404

    with sync_client(app) as member_client:
        assert login(member_client, member_email).status_code == 303

        assert member_client.get(f"{team_url}/search").status_code == 200
        assert member_client.get(f"{team_url}/settings").status_code == 403
        assert member_client.get(f"{team_url}/jobs/new").status_code == 403


async def test_a_search_only_member_lands_on_search_with_no_management_nav(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    app: Litestar,
) -> None:
    leader_email = await _seed_member(pool, "lider@osiptel.test")
    member_email = await _seed_member(pool, "miembro@osiptel.test")

    admin_id = await seed_site_admin(pool, "admin@osiptel.test")
    team = await team_repository.create_first_team(
        "equipo-solo", "Equipo Solo", admin_id
    )
    leader_id = await pool.fetchval(
        "SELECT id FROM portal_users WHERE email = $1", leader_email
    )
    member_id = await pool.fetchval(
        "SELECT id FROM portal_users WHERE email = $1", member_email
    )
    await team_repository.add_member(team.id, leader_id, TeamRole.TEAM_LEADER)
    await team_repository.add_member(team.id, member_id, TeamRole.TEAM_MEMBER)

    with sync_client(app) as member_client:
        assert login(member_client, member_email).status_code == 303

        landing = member_client.get("/", follow_redirects=False)
        assert landing.status_code == 303
        assert landing.headers["location"] == f"/teams/{team.id}/search"

        page = member_client.get(landing.headers["location"])
        assert "Equipos" not in page.text
        assert "Actividad" not in page.text
        assert 'class="sidebar sidebar--minimal"' in page.text

    with sync_client(app) as leader_client:
        assert login(leader_client, leader_email).status_code == 303

        page = leader_client.get("/")
        assert 'class="sidebar sidebar--minimal"' not in page.text
        assert "Equipos" in page.text


async def test_ensure_first_team_creates_once_and_verifies_on_rerun(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    provisioning: ProvisioningService,
) -> None:
    admin_id = await seed_site_admin(pool, "bootstrap@osiptel.test")

    created = await provisioning.ensure_first_team(
        admin_id,
        name="Equipo Inicial",
        slug="equipo-inicial",
    )

    assert created.created is True
    assert (
        await team_repository.role_for(admin_id, created.team.id)
        is TeamRole.TEAM_LEADER
    )

    rerun = await provisioning.ensure_first_team(
        admin_id,
        name="Equipo Inicial",
        slug="equipo-inicial",
    )

    assert rerun.created is False
    assert rerun.team.id == created.team.id
    assert (
        await team_repository.role_for(admin_id, rerun.team.id) is TeamRole.TEAM_LEADER
    )


async def test_ensure_first_team_rejects_a_mismatched_rerun(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    provisioning: ProvisioningService,
) -> None:
    admin_id = await seed_site_admin(pool, "bootstrap@osiptel.test")

    await team_repository.create_first_team(
        "equipo-inicial",
        "Equipo Inicial",
        admin_id,
    )

    with pytest.raises(ProvisioningError) as excinfo:
        await provisioning.ensure_first_team(
            admin_id,
            name="Otro Equipo",
            slug="otro-equipo",
        )

    assert excinfo.value.reason is Reason.INITIAL_TEAM_MISMATCH


async def test_the_first_administrator_is_created_pending_their_own_setup(
    pool: asyncpg.Pool,
    provisioning: ProvisioningService,
) -> None:
    hashed = hash_password(PASSWORD)

    administrator, needs_setup = await provisioning.ensure_site_admin(
        "bootstrap@osiptel.test",
        hashed,
    )

    # Never a site admin yet, and no secret was generated here for an
    # operator to see: only the account exists, waiting on /security/setup.
    assert administrator.is_site_admin is False
    assert administrator.pending_site_admin is True
    assert administrator.mfa_enabled is False
    assert administrator.has_passkey is False
    assert needs_setup is True

    # Rerunning provisioning must not disturb a pending account, and once the
    # account completes its own enrollment, rerunning must not touch it either.
    again, still_pending = await provisioning.ensure_site_admin(
        "bootstrap@osiptel.test",
        hashed,
    )

    assert again.id == administrator.id
    assert still_pending is True

    setup = await provisioning.begin_totp_setup(administrator.id)
    code = pyotp.TOTP(_secret_from_uri(setup.enrollment_uri)).now()
    recovery_codes = await provisioning.confirm_totp_setup(
        administrator.id,
        setup_token=setup.setup_token,
        code=code,
    )

    assert recovery_codes is not None
    assert len(set(recovery_codes)) == RECOVERY_CODE_COUNT

    promoted = await provisioning.user_detail(administrator.id, administrator.id)
    assert promoted.is_site_admin is True
    assert promoted.pending_site_admin is False
    assert promoted.mfa_enabled is True

    settled, no_longer_pending = await provisioning.ensure_site_admin(
        "bootstrap@osiptel.test",
        hashed,
    )

    assert settled.id == administrator.id
    assert no_longer_pending is False


def _secret_from_uri(enrollment_uri: str) -> str:
    return parse_qs(urlparse(enrollment_uri).query)["secret"][0]


async def test_a_site_admin_cannot_exist_without_a_second_factor(
    pool: asyncpg.Pool,
) -> None:
    user_id = await seed_user(pool, email="sin-mfa@osiptel.test")

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pool.execute(
            "UPDATE portal_users SET is_site_admin = true WHERE id = $1",
            user_id,
        )


def _now() -> datetime:
    return datetime.now(UTC)


async def test_deactivating_a_teams_sole_leader_is_blocked_and_names_the_team(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    provisioning: ProvisioningService,
) -> None:
    admin_id = await seed_site_admin(pool, "admin@osiptel.test")
    team = await seed_team(pool)

    with pytest.raises(ProvisioningError) as raised:
        await provisioning.deactivate_user(
            admin_id,
            user_id=team.actor_id,
            mfa_verified_at=_now(),
            trace=RequestTrace(),
        )

    assert raised.value.reason is Reason.USER_LAST_LEADER
    assert raised.value.params["teams"] == "Equipo"

    # Adding a second leader clears the way, the same escape hatch a stuck
    # team is meant to have.
    second_leader = await seed_user(pool, email="segunda-lider@osiptel.test")
    await team_repository.add_member(
        team.team_id,
        second_leader,
        TeamRole.TEAM_LEADER,
    )

    deactivated = await provisioning.deactivate_user(
        admin_id,
        user_id=team.actor_id,
        mfa_verified_at=_now(),
        trace=RequestTrace(),
    )

    assert deactivated.is_active is False


async def test_deactivating_the_last_active_site_admin_is_blocked(
    pool: asyncpg.Pool,
    provisioning: ProvisioningService,
) -> None:
    admin_id = await seed_site_admin(pool, "unico@osiptel.test")
    other_admin_id = await seed_site_admin(pool, "otro@osiptel.test")

    # With two admins, either can deactivate the other.
    await provisioning.deactivate_user(
        admin_id,
        user_id=other_admin_id,
        mfa_verified_at=_now(),
        trace=RequestTrace(),
    )

    reactivated = await provisioning.reactivate_user(
        admin_id,
        user_id=other_admin_id,
        mfa_verified_at=_now(),
        trace=RequestTrace(),
    )
    assert reactivated.is_active is True


async def test_the_installation_must_retain_an_active_site_admin(
    pool: asyncpg.Pool,
) -> None:
    """The real backstop for "never zero active admins".

    Unreachable through deactivate_user/demote_site_admin in a single
    request: the actor must itself be an active admin and can't target
    itself, so a different, active admin target always leaves the actor
    standing. This asserts the deferred constraint trigger that catches the
    concurrent-request case those two guards can't reason about, the same
    way portal_team_must_have_leader backstops _check_not_last_leader.
    """
    admin_id = await seed_site_admin(pool, "unico@osiptel.test")

    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pool.execute(
            "UPDATE portal_users SET is_active = false WHERE id = $1",
            admin_id,
        )


async def test_an_admin_cannot_act_on_their_own_account(
    pool: asyncpg.Pool,
    provisioning: ProvisioningService,
) -> None:
    admin_id = await seed_site_admin(pool, "admin@osiptel.test")

    for coro in (
        provisioning.deactivate_user(
            admin_id, user_id=admin_id, mfa_verified_at=_now(), trace=RequestTrace()
        ),
        provisioning.delete_user(
            admin_id, user_id=admin_id, mfa_verified_at=_now(), trace=RequestTrace()
        ),
    ):
        with pytest.raises(ProvisioningError) as raised:
            await coro

        assert raised.value.reason is Reason.USER_CANNOT_DEACTIVATE_SELF


async def test_deleting_a_user_requires_zero_history(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    provisioning: ProvisioningService,
) -> None:
    admin_id = await seed_site_admin(pool, "admin@osiptel.test")
    team = await seed_team(pool)
    unused = await seed_user(pool, email="sin-uso@osiptel.test")

    # A second leader clears the sole-leader guard, isolating the assertion
    # below to the history check it's meant to exercise.
    second_leader = await seed_user(pool, email="segunda-lider@osiptel.test")
    await team_repository.add_member(
        team.team_id,
        second_leader,
        TeamRole.TEAM_LEADER,
    )

    with pytest.raises(ProvisioningError) as raised:
        await provisioning.delete_user(
            admin_id,
            user_id=team.actor_id,
            mfa_verified_at=_now(),
            trace=RequestTrace(),
        )
    assert raised.value.reason is Reason.USER_HAS_HISTORY

    await provisioning.delete_user(
        admin_id,
        user_id=unused,
        mfa_verified_at=_now(),
        trace=RequestTrace(),
    )

    with pytest.raises(NotFound):
        await provisioning.user_detail(admin_id, unused)


async def test_promoting_marks_pending_until_self_enrollment_completes(
    pool: asyncpg.Pool,
    provisioning: ProvisioningService,
) -> None:
    admin_id = await seed_site_admin(pool, "admin@osiptel.test")
    candidate_id = await seed_user(pool, email="candidata@osiptel.test")

    needs_setup = await provisioning.promote_to_site_admin(
        admin_id,
        user_id=candidate_id,
        mfa_verified_at=_now(),
        trace=RequestTrace(),
    )

    assert needs_setup is True

    pending = await provisioning.user_detail(admin_id, candidate_id)
    # Not an admin yet, and the promoting admin never sees the candidate's
    # own second factor: only the candidate can produce it, at /security/setup.
    assert pending.is_site_admin is False
    assert pending.pending_site_admin is True

    setup = await provisioning.begin_totp_setup(candidate_id)
    code = pyotp.TOTP(_secret_from_uri(setup.enrollment_uri)).now()
    recovery_codes = await provisioning.confirm_totp_setup(
        candidate_id,
        setup_token=setup.setup_token,
        code=code,
    )

    assert recovery_codes is not None
    assert len(set(recovery_codes)) == RECOVERY_CODE_COUNT

    promoted = await provisioning.user_detail(admin_id, candidate_id)
    assert promoted.is_site_admin is True
    assert promoted.pending_site_admin is False
    assert promoted.mfa_enabled is True

    # Promoting an already-promoted account is a no-op.
    again = await provisioning.promote_to_site_admin(
        admin_id,
        user_id=candidate_id,
        mfa_verified_at=_now(),
        trace=RequestTrace(),
    )
    assert again is False

    # Two active admins, so demoting one is allowed; the guard that blocks
    # stripping the *last* one is covered by the deactivation test above,
    # which shares the same _require_not_last_site_admin check.
    demoted = await provisioning.demote_site_admin(
        admin_id,
        user_id=candidate_id,
        mfa_verified_at=_now(),
        trace=RequestTrace(),
    )
    assert demoted.is_site_admin is False


async def test_promoting_someone_who_already_self_enrolled_skips_the_pending_state(
    pool: asyncpg.Pool,
    provisioning: ProvisioningService,
) -> None:
    admin_id = await seed_site_admin(pool, "admin@osiptel.test")
    candidate_id = await seed_user(pool, email="ya-configurada@osiptel.test")

    setup = await provisioning.begin_totp_setup(candidate_id)
    code = pyotp.TOTP(_secret_from_uri(setup.enrollment_uri)).now()
    await provisioning.confirm_totp_setup(
        candidate_id,
        setup_token=setup.setup_token,
        code=code,
    )

    needs_setup = await provisioning.promote_to_site_admin(
        admin_id,
        user_id=candidate_id,
        mfa_verified_at=_now(),
        trace=RequestTrace(),
    )

    assert needs_setup is False

    promoted = await provisioning.user_detail(admin_id, candidate_id)
    assert promoted.is_site_admin is True
    assert promoted.pending_site_admin is False


async def test_a_deactivated_persons_session_stops_working_immediately(
    pool: asyncpg.Pool,
    team_repository: PostgresTeamRepository,
    settings: PortalSettings,
    app: Litestar,
) -> None:
    admin_email = "admin@osiptel.test"
    leader_email = await _seed_member(pool, "lider@osiptel.test")
    admin_id = await seed_site_admin(pool, admin_email)

    team = await team_repository.create_first_team(
        "equipo-cutoff", "Equipo Cutoff", admin_id
    )
    leader_id = await pool.fetchval(
        "SELECT id FROM portal_users WHERE email = $1", leader_email
    )
    await team_repository.add_member(team.id, leader_id, TeamRole.TEAM_LEADER)

    # Two TestClients against the same app share one asyncpg pool bound to
    # one event loop; nesting their `with` blocks runs each on its own
    # blocking portal and trips asyncpg's "different loop" guard. Sequential
    # blocks avoid that, so the leader's cookie is captured, then replayed
    # after the admin acts.
    with sync_client(app) as leader_client:
        assert login(leader_client, leader_email).status_code == 303
        assert leader_client.get("/").status_code == 200
        session_cookie = leader_client.cookies[settings.session_cookie]

    with sync_client(app) as admin_client:
        assert login(admin_client, admin_email).status_code == 303
        page = admin_client.get(f"/admin/users/{leader_id}")

        response = admin_client.post(
            f"/admin/users/{leader_id}",
            data={"action": "deactivate", "csrf_token": csrf_token(page.text)},
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        assert response.status_code == 303

    with sync_client(app) as replay_client:
        replay_client.cookies.set(settings.session_cookie, session_cookie)

        # Same cookie as before, now dead: the session is re-read from
        # Postgres on every request, not cached at login.
        blocked = replay_client.get("/", follow_redirects=False)
        assert blocked.status_code == 303
        assert blocked.headers["location"] == "/login"
