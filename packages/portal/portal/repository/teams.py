from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from portal.domain.errors import ProvisioningError, Reason
from portal.domain.models import PortalUser, Team, TeamInvite, TeamRole
from portal.repository.shared import has_passkey_sql, lock_team_row, user_row


if TYPE_CHECKING:
    from datetime import datetime

    from asyncpg import Connection, Pool, Record


def team_row(row: Record) -> Team:
    return Team(
        row["id"],
        row["slug"],
        row["name"],
        has_global_search=row["has_global_search"],
    )


def _invite_row(row: Record) -> TeamInvite:
    return TeamInvite(
        id=row["id"],
        team_id=row["team_id"],
        email=row["email"],
        role=TeamRole(row["role"]),
        invited_by=row["invited_by"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        accepted_at=row["accepted_at"],
    )


class PostgresTeamRepository:
    """Teams, memberships, and site-wide installation state."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def role_for(self, actor_id: UUID, team_id: UUID) -> TeamRole | None:
        async with self._pool.acquire() as connection:
            role = await connection.fetchval(
                """
                SELECT role FROM portal_team_memberships
                 WHERE team_id = $1 AND user_id = $2
                """,
                team_id,
                actor_id,
            )
        return TeamRole(role) if role else None

    async def teams_for_user(self, actor_id: UUID) -> tuple[Team, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT team.id, team.slug, team.name, team.has_global_search,
                       membership.role
                  FROM portal_teams AS team
                  JOIN portal_team_memberships AS membership
                    ON membership.team_id = team.id
                 WHERE membership.user_id = $1
                 ORDER BY team.name
                """,
                actor_id,
            )
        return tuple(
            Team(
                row["id"],
                row["slug"],
                row["name"],
                TeamRole(row["role"]),
                has_global_search=row["has_global_search"],
            )
            for row in rows
        )

    async def team(self, team_id: UUID) -> Team | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT id, slug, name, has_global_search FROM portal_teams WHERE id = $1",
                team_id,
            )
        return team_row(row) if row else None

    async def team_by_slug(self, slug: str) -> Team | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT id, slug, name, has_global_search FROM portal_teams WHERE slug = $1",
                slug,
            )
        return team_row(row) if row else None

    async def all_teams(self) -> tuple[Team, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT id, slug, name, has_global_search FROM portal_teams ORDER BY name"
            )
        return tuple(team_row(row) for row in rows)

    async def set_global_search(self, team_id: UUID, *, enabled: bool) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "UPDATE portal_teams SET has_global_search = $2 WHERE id = $1",
                team_id,
                enabled,
            )

    async def any_team_has_global_search(self, actor_id: UUID) -> bool:
        async with self._pool.acquire() as connection:
            flag = await connection.fetchval(
                """
                SELECT bool_or(team.has_global_search)
                  FROM portal_teams AS team
                  JOIN portal_team_memberships AS membership
                    ON membership.team_id = team.id
                 WHERE membership.user_id = $1
                """,
                actor_id,
            )
        return bool(flag)

    async def users(self) -> tuple[PortalUser, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT id, email, is_site_admin, is_active, mfa_enabled,
                       pending_site_admin, {has_passkey_sql()}
                  FROM portal_users
                 ORDER BY email
                """
            )
        return tuple(user_row(row) for row in rows)

    async def teams_for_user_detail(self, user_id: UUID) -> tuple[Team, ...]:
        """Every team a user belongs to, for the admin user-detail page."""
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT team.id, team.slug, team.name, membership.role
                  FROM portal_teams AS team
                  JOIN portal_team_memberships AS membership
                    ON membership.team_id = team.id
                 WHERE membership.user_id = $1
                 ORDER BY team.name
                """,
                user_id,
            )
        return tuple(
            Team(row["id"], row["slug"], row["name"], TeamRole(row["role"]))
            for row in rows
        )

    async def teams_where_sole_leader(self, user_id: UUID) -> tuple[Team, ...]:
        """Teams that would lose their last leader if user_id were removed.

        Used to block deactivating an account before another leader is in
        place, the same guarantee _check_not_last_leader already gives
        membership changes.
        """
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT team.id, team.slug, team.name, team.has_global_search
                  FROM portal_teams AS team
                  JOIN portal_team_memberships AS membership
                    ON membership.team_id = team.id
                   AND membership.user_id = $1
                   AND membership.role = 'team_leader'
                 WHERE (
                     SELECT count(*)
                       FROM portal_team_memberships AS other
                      WHERE other.team_id = team.id
                        AND other.role = 'team_leader'
                 ) <= 1
                 ORDER BY team.name
                """,
                user_id,
            )
        return tuple(team_row(row) for row in rows)

    async def is_site_admin(self, user_id: UUID) -> bool:
        async with self._pool.acquire() as connection:
            flag = await connection.fetchval(
                """
                SELECT is_site_admin
                  FROM portal_users
                 WHERE id = $1
                   AND is_active
                """,
                user_id,
            )
        return bool(flag)

    async def installation_status(self) -> tuple[int, UUID | None]:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT initial_team_id
                  FROM portal_installation_state
                 WHERE singleton = true
                """
            )
            count = await connection.fetchval("SELECT count(*) FROM portal_teams")

        return int(count), row["initial_team_id"] if row else None

    async def create_first_team(
        self,
        slug: str,
        name: str,
        actor_id: UUID,
    ) -> Team:
        team = Team(uuid4(), slug, name)

        async with self._pool.acquire() as connection, connection.transaction():
            state = await connection.fetchrow(
                """
                SELECT initial_team_id
                  FROM portal_installation_state
                 WHERE singleton = true
                 FOR UPDATE
                """
            )
            count = await connection.fetchval("SELECT count(*) FROM portal_teams")

            if state is None or state["initial_team_id"] is not None or int(count):
                raise ProvisioningError(Reason.INITIAL_TEAM_EXISTS)

            await connection.execute(
                """
                INSERT INTO portal_teams (id, slug, name, created_by)
                VALUES ($1, $2, $3, $4)
                """,
                team.id,
                team.slug,
                team.name,
                actor_id,
            )
            await connection.execute(
                """
                INSERT INTO portal_team_memberships (team_id, user_id, role)
                VALUES ($1, $2, 'team_leader')
                """,
                team.id,
                actor_id,
            )
            await connection.execute(
                """
                UPDATE portal_installation_state
                   SET initial_team_id = $1,
                       completed_by = $2,
                       completed_at = now()
                 WHERE singleton = true
                """,
                team.id,
                actor_id,
            )

        return team

    async def create_team(
        self,
        slug: str,
        name: str,
        created_by: UUID,
        leader_id: UUID,
    ) -> Team:
        team = Team(uuid4(), slug, name)

        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """
                INSERT INTO portal_teams (id, slug, name, created_by)
                VALUES ($1, $2, $3, $4)
                """,
                team.id,
                team.slug,
                team.name,
                created_by,
            )
            await connection.execute(
                """
                INSERT INTO portal_team_memberships (team_id, user_id, role)
                VALUES ($1, $2, 'team_leader')
                """,
                team.id,
                leader_id,
            )

        return team

    async def add_member(
        self,
        team_id: UUID,
        user_id: UUID,
        role: TeamRole,
    ) -> None:
        if role not in {TeamRole.TEAM_LEADER, TeamRole.TEAM_MEMBER}:
            raise ProvisioningError(Reason.ROLE_INVALID)

        async with self._pool.acquire() as connection, connection.transaction():
            await lock_team_row(connection, team_id)

            current = await connection.fetchval(
                """
                SELECT role
                  FROM portal_team_memberships
                 WHERE team_id = $1
                   AND user_id = $2
                 FOR UPDATE
                """,
                team_id,
                user_id,
            )

            if (
                current == TeamRole.TEAM_LEADER.value
                and role is not TeamRole.TEAM_LEADER
            ):
                await self._check_not_last_leader(connection, team_id)

            await connection.execute(
                """
                INSERT INTO portal_team_memberships (team_id, user_id, role)
                VALUES ($1, $2, $3)
                ON CONFLICT (team_id, user_id)
                DO UPDATE SET role = EXCLUDED.role
                """,
                team_id,
                user_id,
                role.value,
            )

    async def remove_member(self, team_id: UUID, user_id: UUID) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            await lock_team_row(connection, team_id)

            current = await connection.fetchval(
                """
                SELECT role
                  FROM portal_team_memberships
                 WHERE team_id = $1
                   AND user_id = $2
                 FOR UPDATE
                """,
                team_id,
                user_id,
            )

            if current == TeamRole.TEAM_LEADER.value:
                await self._check_not_last_leader(connection, team_id)

            await connection.execute(
                """
                DELETE FROM portal_team_memberships
                 WHERE team_id = $1
                   AND user_id = $2
                """,
                team_id,
                user_id,
            )

    async def members_for_team(
        self,
        team_id: UUID,
    ) -> tuple[tuple[PortalUser, TeamRole], ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                f"""
                SELECT user_account.id,
                       user_account.email,
                       user_account.is_site_admin,
                       user_account.is_active,
                       user_account.mfa_enabled,
                       user_account.pending_site_admin,
                       {has_passkey_sql("user_account")},
                       membership.role
                  FROM portal_team_memberships AS membership
                  JOIN portal_users AS user_account
                    ON user_account.id = membership.user_id
                 WHERE membership.team_id = $1
                 ORDER BY user_account.email
                """,
                team_id,
            )

        return tuple((user_row(row), TeamRole(row["role"])) for row in rows)

    async def create_invite(
        self,
        team_id: UUID,
        email: str,
        role: TeamRole,
        *,
        token_hash: str,
        invited_by: UUID,
        expires_at: datetime,
    ) -> TeamInvite:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO portal_team_invites (
                    id, team_id, email, role, token_hash, invited_by, expires_at
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (team_id, email) DO UPDATE
                    SET id = EXCLUDED.id,
                        role = EXCLUDED.role,
                        token_hash = EXCLUDED.token_hash,
                        invited_by = EXCLUDED.invited_by,
                        created_at = now(),
                        expires_at = EXCLUDED.expires_at,
                        accepted_at = NULL
                RETURNING id, team_id, email, role, invited_by, created_at,
                          expires_at, accepted_at
                """,
                uuid4(),
                team_id,
                email,
                role.value,
                token_hash,
                invited_by,
                expires_at,
            )

        return _invite_row(row)

    async def invite_by_token_hash(self, token_hash: str) -> TeamInvite | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT id, team_id, email, role, invited_by, created_at,
                       expires_at, accepted_at
                  FROM portal_team_invites
                 WHERE token_hash = $1
                """,
                token_hash,
            )

        return _invite_row(row) if row is not None else None

    async def pending_invites_for_team(self, team_id: UUID) -> tuple[TeamInvite, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT id, team_id, email, role, invited_by, created_at,
                       expires_at, accepted_at
                  FROM portal_team_invites
                 WHERE team_id = $1
                   AND accepted_at IS NULL
                 ORDER BY created_at
                """,
                team_id,
            )

        return tuple(_invite_row(row) for row in rows)

    async def delete_invite(self, team_id: UUID, invite_id: UUID) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM portal_team_invites WHERE id = $1 AND team_id = $2",
                invite_id,
                team_id,
            )

    async def mark_invite_accepted(self, invite_id: UUID) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "UPDATE portal_team_invites SET accepted_at = now() WHERE id = $1",
                invite_id,
            )

    async def _check_not_last_leader(
        self,
        connection: Connection,
        team_id: UUID,
    ) -> None:
        leaders = await connection.fetchval(
            """
            SELECT count(*)
              FROM portal_team_memberships
             WHERE team_id = $1
               AND role = 'team_leader'
            """,
            team_id,
        )

        if int(leaders) <= 1:
            raise ProvisioningError(Reason.LAST_LEADER)
