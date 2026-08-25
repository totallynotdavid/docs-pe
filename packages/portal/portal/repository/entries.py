from __future__ import annotations

import json

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from portal.domain.models import Entry


if TYPE_CHECKING:
    from datetime import timedelta
    from uuid import UUID

    from asyncpg import Pool, Record


def entry_row(row: Record) -> Entry:
    return Entry(
        id=row["id"],
        document=row["document"],
        source=row["source"],
        status=row["status"],
        columns=tuple(row["columns"]),
        rows=tuple(tuple(line) for line in json.loads(row["rows"])),
        error_code=row["error_code"],
        first_seen_at=row["first_seen_at"],
        last_confirmed_at=row["last_confirmed_at"],
    )


# Entries with these statuses count as a usable answer for the submission
# review's "already known" check. 'failed' never does: a failed attempt has
# nothing to hand back, no matter how recent, so it always falls through to
# a fresh fetch.
_REUSABLE_STATUSES = ("ok", "not_found")


class PostgresEntryRepository:
    """Reads over the deduplicated, cross-team portal_entries store.

    Writes happen inside PostgresJobRepository.publish(), in the same
    transaction as the job_items fencing check -- an entry only ever becomes
    visible together with the job_item that confirmed it. This module owns
    every read path instead: team-scoped search, global search, single-entry
    lookup, and the submission-review reuse check.
    """

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def search_team(
        self,
        team_id: UUID,
        needle: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[tuple[Entry, ...], bool]:
        """Entries this team has ever confirmed, matching needle.

        Scoped through portal_job_items.team_id, never through a filter on
        portal_entries alone: an entry another team confirmed must never
        surface here just because the document happens to match.
        """
        rows = await self._pool.fetch(
            """
            SELECT entries.*
              FROM portal_entries AS entries
             WHERE entries.document ILIKE '%' || $2 || '%'
               AND EXISTS (
                   SELECT 1
                     FROM portal_job_items AS item
                    WHERE item.team_id = $1
                      AND item.entry_id = entries.id
               )
             ORDER BY entries.last_confirmed_at DESC
             LIMIT $3
            OFFSET $4
            """,
            team_id,
            needle,
            limit + 1,
            offset,
        )

        return tuple(entry_row(row) for row in rows[:limit]), len(rows) > limit

    async def search_global(
        self,
        needle: str,
        *,
        limit: int,
        offset: int,
    ) -> tuple[tuple[Entry, ...], bool]:
        """Every entry matching needle, regardless of which team confirmed
        it. Callers must gate access themselves -- see
        AuthorizedService._require_global_search."""
        rows = await self._pool.fetch(
            """
            SELECT *
              FROM portal_entries
             WHERE document ILIKE '%' || $1 || '%'
             ORDER BY last_confirmed_at DESC
             LIMIT $2
            OFFSET $3
            """,
            needle,
            limit + 1,
            offset,
        )

        return tuple(entry_row(row) for row in rows[:limit]), len(rows) > limit

    async def entry_for_team(self, team_id: UUID, entry_id: UUID) -> Entry | None:
        """A single entry, only if this team has itself confirmed it."""
        row = await self._pool.fetchrow(
            """
            SELECT entries.*
              FROM portal_entries AS entries
             WHERE entries.id = $2
               AND EXISTS (
                   SELECT 1
                     FROM portal_job_items AS item
                    WHERE item.team_id = $1
                      AND item.entry_id = entries.id
               )
            """,
            team_id,
            entry_id,
        )

        return entry_row(row) if row is not None else None

    async def entry_by_id(self, entry_id: UUID) -> Entry | None:
        """Unscoped lookup for global search. Gate access before calling."""
        row = await self._pool.fetchrow(
            "SELECT * FROM portal_entries WHERE id = $1",
            entry_id,
        )

        return entry_row(row) if row is not None else None

    async def reusable_for_team(
        self,
        team_id: UUID,
        pairs: frozenset[tuple[str, str]],
        *,
        freshness: dict[str, timedelta],
    ) -> dict[tuple[str, str], UUID]:
        """Of `pairs` (document, source), the ones this team already has a
        fresh, usable answer for, mapped to the existing entry's id.

        Scoped to this team's own portal_job_items throughout: a document
        another team scanned is never offered back here, no matter how
        fresh, so reuse can never let one team draw on another's proxy spend.
        """
        if not pairs:
            return {}

        documents = list({document for document, _source in pairs})
        sources = list({source for _document, source in pairs})

        rows = await self._pool.fetch(
            """
            SELECT item.document, item.source, item.entry_id,
                   entries.status, entries.last_confirmed_at
              FROM portal_job_items AS item
              JOIN portal_entries AS entries ON entries.id = item.entry_id
             WHERE item.team_id = $1
               AND item.state = 'published'
               AND item.document = ANY($2::text[])
               AND item.source = ANY($3::text[])
            """,
            team_id,
            documents,
            sources,
        )

        now = datetime.now(UTC)
        result: dict[tuple[str, str], UUID] = {}

        for row in rows:
            pair = (row["document"], row["source"])

            if pair not in pairs or row["status"] not in _REUSABLE_STATUSES:
                continue

            window = freshness.get(pair[1])

            if window is not None and now - row["last_confirmed_at"] <= window:
                result[pair] = row["entry_id"]

        return result
