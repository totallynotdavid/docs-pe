from __future__ import annotations

from typing import TYPE_CHECKING

from portal.repository.workers import HEARTBEAT_STALE_AFTER


if TYPE_CHECKING:
    from asyncpg import Pool


# A worker row is only ever removed via portal_workers' own deletion, which
# already SETs portal_proxy_slots.worker_id NULL through the FK (ON DELETE SET
# NULL): a leased row's worker_id always still resolves to a portal_workers
# row here, so this is a plain (not outer) join.
_REAP_STALE = """
UPDATE portal_proxy_slots AS s
   SET worker_id = NULL, lane_index = NULL, leased_at = NULL
  FROM portal_workers AS w
 WHERE s.provider = $1
   AND s.worker_id = w.worker_id
   AND (
        w.revoked_at IS NOT NULL
     OR w.last_seen_at < now() - $2::interval
     OR (w.last_seen_at IS NULL AND s.leased_at < now() - $2::interval)
   )
"""

# Deliberately no JOIN here. Tried joining portal_workers into this query to
# do the staleness check inline: FOR UPDATE SKIP LOCKED combined with a join
# on its locked side does not reliably prevent two concurrent claims from
# picking the same row, confirmed empirically. _REAP_STALE runs first, in the
# same transaction, so this only ever needs the table's own worker_id column.
_CLAIM = """
WITH candidate AS (
    SELECT slot_id
      FROM portal_proxy_slots
     WHERE provider = $1
       AND worker_id IS NULL
     ORDER BY slot_id
     FOR UPDATE SKIP LOCKED
     LIMIT 1
)
UPDATE portal_proxy_slots AS s
   SET worker_id = $2, lane_index = $3, leased_at = now()
  FROM candidate
 WHERE s.provider = $1
   AND s.slot_id = candidate.slot_id
RETURNING s.slot_id
"""

_RELEASE = """
UPDATE portal_proxy_slots
   SET worker_id = NULL, lane_index = NULL, leased_at = NULL
 WHERE provider = $1
   AND slot_id = $2
   AND worker_id = $3
"""


class PostgresProxySlots:
    """Fleet-wide assignment of provider slot_id values to real ports.

    fetch/proxy/geonode.py maps slot_id to one of GeoNode's 901 sticky ports;
    that mapping only stays unique if every lane, on every worker node, holds
    a distinct slot_id at once. A lane claims a free row the first time it
    works a job for a given provider and holds it until it switches provider
    or goes idle, independent of how often the underlying provider session
    itself gets closed and reopened. A row whose owning worker has gone stale
    (past HEARTBEAT_STALE_AFTER) or was revoked is treated as free.
    """

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def claim(
        self, *, provider: str, worker_id: str, lane_index: int
    ) -> int | None:
        async with self._pool.acquire() as conn, conn.transaction():
            await conn.execute(_REAP_STALE, provider, HEARTBEAT_STALE_AFTER)
            slot_id = await conn.fetchval(_CLAIM, provider, worker_id, lane_index)

        return None if slot_id is None else int(slot_id)

    async def release(self, *, provider: str, slot_id: int, worker_id: str) -> None:
        await self._pool.execute(_RELEASE, provider, slot_id, worker_id)
