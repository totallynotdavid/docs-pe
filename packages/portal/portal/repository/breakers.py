from __future__ import annotations

from typing import TYPE_CHECKING

from core.pipeline.breaker import (
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

# A single atomic UPDATE, not a SELECT ... FOR UPDATE followed by a
# Python-side branch: the earlier version held the row lock across an
# extra network round trip, and every lookup for a given (source, provider)
# contends on the same row, so that gap serializes the whole fleet the same
# way the queue gate lock once did. $3 = healthy_contact, $4 = threshold,
# $5 = base_cooldown_s, $6 = max_cooldown_s.
_RECORD_OUTCOME = """
UPDATE portal_circuit_breakers b
   SET consecutive_failures = CASE
           WHEN $3 THEN 0
           WHEN b.consecutive_failures + 1 < $4 THEN b.consecutive_failures + 1
           ELSE 0
       END,
       level = CASE
           WHEN $3 THEN 0
           WHEN b.consecutive_failures + 1 < $4 THEN b.level
           ELSE b.level + 1
       END,
       open_until = CASE
           WHEN $3 THEN NULL
           WHEN b.consecutive_failures + 1 < $4 THEN b.open_until
           ELSE now() + (LEAST($6, $5 * power(2, b.level)) * interval '1 second')
       END
 WHERE b.source = $1 AND b.provider = $2
"""


class PostgresCircuitBreakers:
    """Persist breaker state so claims exclude open site-provider pairs fleet-wide."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def record_outcome(
        self, *, source: str, provider: str, healthy_contact: bool
    ) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(_ENSURE_ROW, source, provider)
            await connection.execute(
                _RECORD_OUTCOME,
                source,
                provider,
                healthy_contact,
                DEFAULT_THRESHOLD,
                DEFAULT_BASE_COOLDOWN_S,
                DEFAULT_MAX_COOLDOWN_S,
            )
