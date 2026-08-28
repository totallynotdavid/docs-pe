from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Sequence

    from asyncpg import Pool


# A held slot must be renewed (via /heartbeat) before this elapses or it
# becomes reclaimable by anyone. Long enough to absorb a few missed
# heartbeats (HEARTBEAT_INTERVAL_SECONDS in worker/agent.py is 15s) without
# losing the slot mid-session; short enough that a worker that crashed,
# was OOM-killed, or never comes back stops blocking the pool within a
# minute instead of forever.
LEASE_TTL = timedelta(seconds=60)

# A slot's own lease_expires_at is the sole authority on reclaimability.
# Earlier versions judged staleness by joining portal_workers.last_seen_at
# instead, which broke on every restart: worker_id is a stable identity that
# outlives any one process, so a freshly restarted process's first heartbeat
# renewed that worker_id's liveness and silently resurrected slots its
# crashed predecessor had abandoned, with no operator-visible signal. A
# lease that must be renewed by name (see renew()) doesn't have that
# failure mode: a restarted process starts holding nothing, so it renews
# nothing, and whatever the old process leaked simply expires on schedule.
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

# unnest($2, $3) pairs each held (provider, slot_id) positionally; a caller
# renewing slots across two providers in one call gets both rows touched in
# one round trip instead of one UPDATE per slot.
_RENEW = """
UPDATE portal_proxy_slots AS s
   SET lease_expires_at = now() + $4::interval
  FROM unnest($2::text[], $3::int[]) AS held(provider, slot_id)
 WHERE s.worker_id = $1
   AND s.provider = held.provider
   AND s.slot_id = held.slot_id
"""


class PostgresProxySlots:
    """Fleet-wide assignment of provider slot_id values to real ports.

    fetch/proxy/geonode.py maps slot_id to one of GeoNode's 901 sticky ports;
    that mapping only stays unique if every lane, on every worker node, holds
    a distinct slot_id at once. A lane claims a free row the first time it
    works a job for a given provider and holds it until it switches provider
    or goes idle, independent of how often the underlying provider session
    itself gets closed and reopened.

    A claim is a lease, not a reservation: it expires on its own (LEASE_TTL)
    unless the holder keeps renewing it, the same pattern portal_job_items
    already uses for job assignment (repository/jobs.py). The holder is
    responsible for renewal, reported once per heartbeat as the exact set of
    slots its lanes currently hold in memory (see worker/agent.py's
    _heartbeat_loop) -- not a blanket "renew everything tagged with my
    worker_id", which is what let a restarted process revive a crashed
    predecessor's leaked rows under the old worker-heartbeat-staleness
    design. An explicit release() still frees a slot immediately for a
    graceful shutdown; it's an optimization; a slot nobody renews expires
    within LEASE_TTL regardless of why.
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
        """Extend the lease on exactly the (provider, slot_id) pairs the
        caller currently holds. A pair this worker_id doesn't actually hold
        (already reclaimed, or never claimed) matches no row and is a safe
        no-op, the same idempotency release() already relies on."""
        if not held:
            return

        providers = [item[0] for item in held]
        slot_ids = [item[1] for item in held]

        await self._pool.execute(_RENEW, worker_id, providers, slot_ids, LEASE_TTL)
