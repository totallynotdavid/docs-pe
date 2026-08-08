from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from portal.domain.models import SearchLogEntry, TeamSearchActivity


if TYPE_CHECKING:
    from uuid import UUID

    from asyncpg import Pool, Record


class PostgresSearchLogRepository:
    """Append-only record of what teams searched, for usage visibility.

    Team leaders see their own team's raw queries; admins see counts across
    every team but never the query text itself, the same document-level
    privacy boundary search results already keep between teams.
    """

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def record(
        self,
        team_id: UUID,
        actor_id: UUID,
        query: str,
        result_count: int,
    ) -> None:
        await self._pool.execute(
            """
            INSERT INTO portal_search_log (id, team_id, actor_id, query, result_count)
            VALUES ($1, $2, $3, $4, $5)
            """,
            uuid4(),
            team_id,
            actor_id,
            query,
            result_count,
        )

    async def recent_for_team(
        self,
        team_id: UUID,
        *,
        limit: int = 50,
    ) -> tuple[SearchLogEntry, ...]:
        rows = await self._pool.fetch(
            """
            SELECT log.id, log.query, log.result_count, log.created_at,
                   user_account.email AS actor_email
              FROM portal_search_log AS log
              JOIN portal_users AS user_account ON user_account.id = log.actor_id
             WHERE log.team_id = $1
             ORDER BY log.created_at DESC
             LIMIT $2
            """,
            team_id,
            limit,
        )
        return tuple(_entry_row(row) for row in rows)

    async def team_totals(self, *, days: int = 14) -> tuple[TeamSearchActivity, ...]:
        """Every team's search volume, most active first. Counts only: an
        admin overseeing the whole install doesn't need another team's
        document-level query text to judge whether search is being used."""
        rows = await self._pool.fetch(
            """
            SELECT team.id, team.name,
                   count(log.id) AS search_count,
                   max(log.created_at) AS last_searched_at
              FROM portal_teams AS team
              LEFT JOIN portal_search_log AS log
                     ON log.team_id = team.id
                    AND log.created_at >= now() - make_interval(days => $1)
             GROUP BY team.id, team.name
             ORDER BY search_count DESC, team.name
            """,
            days,
        )
        return tuple(
            TeamSearchActivity(
                team_id=row["id"],
                team_name=row["name"],
                search_count=int(row["search_count"]),
                last_searched_at=row["last_searched_at"],
            )
            for row in rows
        )


def _entry_row(row: Record) -> SearchLogEntry:
    return SearchLogEntry(
        id=row["id"],
        query=row["query"],
        result_count=int(row["result_count"]),
        actor_email=row["actor_email"],
        created_at=row["created_at"],
    )
