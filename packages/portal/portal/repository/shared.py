from __future__ import annotations

from typing import TYPE_CHECKING

from portal.domain.errors import NotFound, Reason
from portal.domain.models import PortalUser


if TYPE_CHECKING:
    from uuid import UUID

    from asyncpg import Connection


def user_row(row: object) -> PortalUser:
    return PortalUser(
        id=row["id"],  # type: ignore[index]
        email=row["email"],  # type: ignore[index]
        is_site_admin=bool(row["is_site_admin"]),  # type: ignore[index]
    )


async def lock_team_row(connection: Connection, team_id: UUID) -> None:
    """Serialize per-team mutations (membership and credential changes) on the team row."""
    found = await connection.fetchval(
        "SELECT id FROM portal_teams WHERE id = $1 FOR UPDATE", team_id
    )
    if found is None:
        raise NotFound(Reason.TEAM_MISSING)
