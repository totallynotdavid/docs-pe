from __future__ import annotations

from typing import TYPE_CHECKING

from portal.domain.errors import NotFound, Reason
from portal.domain.models import PortalUser


if TYPE_CHECKING:
    from uuid import UUID

    from asyncpg import Connection, Record


def user_row(row: Record) -> PortalUser:
    return PortalUser(
        id=row["id"],
        email=row["email"],
        is_site_admin=bool(row["is_site_admin"]),
    )


async def lock_team_row(connection: Connection, team_id: UUID) -> None:
    """Serialize membership and credential changes for a team."""
    found = await connection.fetchval(
        "SELECT id FROM portal_teams WHERE id = $1 FOR UPDATE",
        team_id,
    )

    if found is None:
        raise NotFound(Reason.TEAM_MISSING)
