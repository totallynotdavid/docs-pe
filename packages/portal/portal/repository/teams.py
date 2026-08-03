from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from portal.domain.errors import ProvisioningError, Reason
from portal.domain.models import PortalUser, Team, TeamRole
from portal.repository.shared import lock_team_row, user_row


if TYPE_CHECKING:
    from asyncpg import Connection, Pool


def team_row(row: object) -> Team:
    return Team(row["id"], row["slug"], row["name"])


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
                SELECT team.id, team.slug, team.name, membership.role
                  FROM portal_teams AS team
                  JOIN portal_team_memberships AS membership
                    ON membership.team_id = team.id
                 WHERE membership.user_id = $1
                 ORDER BY team.name
                """,
                actor_id,
            )
        return tuple(
            Team(row["id"], row["slug"], row["name"], TeamRole(row["role"]))
            for row in rows
        )

    async def team(self, team_id: UUID) -> Team | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT id, slug, name FROM portal_teams WHERE id = $1",
                team_id,
            )
        return team_row(row) if row else None

    async def team_by_slug(self, slug: str) -> Team | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT id, slug, name FROM portal_teams WHERE slug = $1",
                slug,
            )
        return team_row(row) if row else None

    async def all_teams(self) -> tuple[Team, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT id, slug, name FROM portal_teams ORDER BY name"
            )
        return tuple(team_row(row) for row in rows)

    async def users(self) -> tuple[PortalUser, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                "SELECT id, email, is_site_admin FROM portal_users ORDER BY email"
            )
        return tuple(user_row(row) for row in rows)

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
                """
                SELECT user_account.id,
                       user_account.email,
                       user_account.is_site_admin,
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
