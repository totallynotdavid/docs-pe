from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fetch.pipeline.breaker import (
    DEFAULT_BASE_COOLDOWN_S,
    DEFAULT_MAX_COOLDOWN_S,
    DEFAULT_THRESHOLD,
)


if TYPE_CHECKING:
    from asyncpg import Pool


_ENSURE_ROW = """
INSERT INTO portal_circuit_breakers (source, provider)
VALUES ($1, $2)
ON CONFLICT (source, provider) DO NOTHING
"""

_LOCK_ROW = """
SELECT consecutive_failures, level
  FROM portal_circuit_breakers
 WHERE source = $1 AND provider = $2
 FOR UPDATE
"""

_RECORD_SUCCESS = """
UPDATE portal_circuit_breakers
   SET consecutive_failures = 0, level = 0, open_until = NULL
 WHERE source = $1 AND provider = $2
"""

_RECORD_BELOW_THRESHOLD = """
UPDATE portal_circuit_breakers
   SET consecutive_failures = $3
 WHERE source = $1 AND provider = $2
"""

_TRIP = """
UPDATE portal_circuit_breakers
   SET consecutive_failures = 0, level = $3, open_until = $4
 WHERE source = $1 AND provider = $2
"""


class PostgresCircuitBreakers:
    """Fleet-wide (site, provider) health that every worker node's /claim
    filters against.

    fetch.pipeline.breaker's CircuitBreaker is correct but process-local: a
    single fetch run has one process, so parking every lane after ten
    consecutive failures is fleet-wide by construction. Portal runs the same
    pipeline from N worker processes, each with its own in-process breaker,
    so that invariant silently became "ten consecutive failures per node."
    This table restores it: /publish reports each claimed item's outcome
    here, and /claim's candidate query excludes items for a (source,
    provider) pair whose breaker is currently open, so a trip on one node's
    lane stops every node's lanes from drawing more of that pair's work.
    """

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def record_outcome(
        self, *, source: str, provider: str, healthy_contact: bool
    ) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(_ENSURE_ROW, source, provider)
            row = await connection.fetchrow(_LOCK_ROW, source, provider)

            if healthy_contact:
                await connection.execute(_RECORD_SUCCESS, source, provider)
                return

            consecutive = int(row["consecutive_failures"]) + 1

            if consecutive < DEFAULT_THRESHOLD:
                await connection.execute(
                    _RECORD_BELOW_THRESHOLD, source, provider, consecutive
                )
                return

            level = int(row["level"]) + 1
            cooldown = min(
                DEFAULT_MAX_COOLDOWN_S,
                DEFAULT_BASE_COOLDOWN_S * (2 ** (level - 1)),
            )
            open_until = datetime.now(UTC) + timedelta(seconds=cooldown)

            await connection.execute(_TRIP, source, provider, level, open_until)
