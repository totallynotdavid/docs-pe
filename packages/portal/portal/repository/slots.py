from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Sequence

    from asyncpg import Pool


# An unrenewed slot becomes reclaimable after this interval.
LEASE_TTL = timedelta(seconds=60)

_CLAIM = """
WITH candidate AS (
    SELECT slot_id
      FROM portal_proxy_slots
     WHERE provider = $1
       AND (worker_id IS NULL OR lease_expires_at < now())
     ORDER BY slot_id
     FOR UPDATE SKIP LOCKED
     LIMIT 1
)
UPDATE portal_proxy_slots AS s
   SET worker_id = $2, lane_index = $3, lease_expires_at = now() + $4::interval
  FROM candidate
 WHERE s.provider = $1
   AND s.slot_id = candidate.slot_id
RETURNING s.slot_id
"""

_RELEASE = """
UPDATE portal_proxy_slots
   SET worker_id = NULL, lane_index = NULL, lease_expires_at = NULL
 WHERE provider = $1
   AND slot_id = $2
   AND worker_id = $3
"""

# The provider and slot arrays are paired by position.
_RENEW = """
UPDATE portal_proxy_slots AS s
   SET lease_expires_at = now() + $4::interval
  FROM unnest($2::text[], $3::int[]) AS held(provider, slot_id)
 WHERE s.worker_id = $1
   AND s.provider = held.provider
   AND s.slot_id = held.slot_id
"""


class PostgresProxySlots:
    """Assign fleet-wide provider slots with expiring leases.

    A lane keeps its slot while it works one provider credential. Renewal
    receives the exact slots held by the current process. An unrenewed lease
    expires, while an explicit release makes a slot available immediately.
    """

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def claim(
        self, *, provider: str, worker_id: str, lane_index: int
    ) -> int | None:
        slot_id = await self._pool.fetchval(
            _CLAIM, provider, worker_id, lane_index, LEASE_TTL
        )

        return None if slot_id is None else int(slot_id)

    async def release(self, *, provider: str, slot_id: int, worker_id: str) -> None:
        await self._pool.execute(_RELEASE, provider, slot_id, worker_id)

    async def renew(self, *, worker_id: str, held: Sequence[tuple[str, int]]) -> None:
        """Extend leases for the exact provider and slot pairs held locally."""
        if not held:
            return

        providers = [item[0] for item in held]
        slot_ids = [item[1] for item in held]

        await self._pool.execute(_RENEW, worker_id, providers, slot_ids, LEASE_TTL)
