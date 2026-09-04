from __future__ import annotations

import json

from typing import TYPE_CHECKING
from uuid import uuid4


if TYPE_CHECKING:
    from asyncpg import Pool

    from portal.domain.models import AuditEvent


class PostgresAuditLog:
    """Append-only record of who did what.

    Nothing reads this table back through the application: a trigger and a
    grant both refuse UPDATE and DELETE, so the only operation the portal has
    on its own audit trail is INSERT.
    """

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def record(self, event: AuditEvent) -> None:
        await self._pool.execute(
            """
            INSERT INTO portal_audit_log (
                id,
                actor_id,
                action,
                target_type,
                target_id,
                ip,
                cf_ray_id,
                metadata
            )
            VALUES ($1, $2, $3, $4, $5, $6::inet, $7, $8::jsonb)
            """,
            uuid4(),
            event.actor_id,
            event.action.value,
            event.target_type,
            event.target_id,
            event.trace.ip if event.trace else None,
            event.trace.ray_id if event.trace else None,
            json.dumps(dict(event.metadata), sort_keys=True),
        )
