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
        mfa_enabled=bool(row["mfa_enabled"]),
        has_passkey=bool(row["has_passkey"]),
        is_active=bool(row["is_active"]),
        pending_site_admin=bool(row["pending_site_admin"]),
    )


# Every portal_users query that feeds user_row must select this alongside
# is_site_admin/is_active/mfa_enabled/pending_site_admin: PortalUser.has_passkey
# has no column of its own to select directly.
def has_passkey_sql(users_alias: str = "portal_users") -> str:
    return f"""EXISTS(
        SELECT 1 FROM portal_webauthn_credentials AS credential
         WHERE credential.user_id = {users_alias}.id
    ) AS has_passkey"""


async def lock_team_row(connection: Connection, team_id: UUID) -> None:
    """Serialize membership and credential changes for a team."""
    found = await connection.fetchval(
        "SELECT id FROM portal_teams WHERE id = $1 FOR UPDATE",
        team_id,
    )

    if found is None:
        raise NotFound(Reason.TEAM_MISSING)
