from __future__ import annotations

import asyncio
import contextlib

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import timedelta

    from asyncpg import Pool


SWEEP_INTERVAL_SECONDS = 60.0


class EphemeralStore:
    """Keyed values that expire on their own and are never a system of record.

    Sessions, rate-limit counters, and single-use tokens live here. Truncating
    the table logs everyone out, resets every counter, and voids every flow in
    progress, such as a login form, an MFA or passkey challenge, or a TOTP or
    passkey enrollment. No durable record is lost.

    Values are opaque strings because every access is by exact key, so typed
    columns per caller would add schema churn and no capability.

    Writes check expiry in the same statement, so requests racing for a live
    key have one winner.
    """

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def put_new(self, key: str, value: str, ttl: timedelta) -> bool:
        """Store only if the key is free. False means someone else holds it."""
        stored = await self._pool.fetchval(
            """
            INSERT INTO portal_ephemeral (key, value, expires_at)
            VALUES ($1, $2, now() + $3::interval)
            ON CONFLICT (key) DO UPDATE
                SET value = EXCLUDED.value,
                    expires_at = EXCLUDED.expires_at
                WHERE portal_ephemeral.expires_at <= now()
            RETURNING key
            """,
            key,
            value,
            ttl,
        )

        return stored is not None

    async def replace(self, key: str, value: str, ttl: timedelta) -> bool:
        """Overwrite and extend only if the key is still live."""
        updated = await self._pool.fetchval(
            """
            UPDATE portal_ephemeral
               SET value = $2,
                   expires_at = now() + $3::interval
             WHERE key = $1
               AND expires_at > now()
            RETURNING key
            """,
            key,
            value,
            ttl,
        )

        return updated is not None

    async def read(self, key: str) -> str | None:
        value = await self._pool.fetchval(
            """
            SELECT value
              FROM portal_ephemeral
             WHERE key = $1
               AND expires_at > now()
            """,
            key,
        )

        return None if value is None else str(value)

    async def take(self, key: str) -> str | None:
        """Read and remove in one step, which is what makes a token single-use."""
        value = await self._pool.fetchval(
            """
            DELETE FROM portal_ephemeral
             WHERE key = $1
               AND expires_at > now()
            RETURNING value
            """,
            key,
        )

        return None if value is None else str(value)

    async def increment(self, key: str, ttl: timedelta) -> int:
        """Count one event in a window that starts at the first event.

        The window deadline is only set when the row is created or has already
        expired. A burst cannot keep pushing it out, which is what stops a soft
        lock from becoming a permanent one.
        """
        count = await self._pool.fetchval(
            """
            INSERT INTO portal_ephemeral (key, value, expires_at)
            VALUES ($1, '1', now() + $2::interval)
            ON CONFLICT (key) DO UPDATE
                SET value = CASE
                        WHEN portal_ephemeral.expires_at <= now() THEN '1'
                        ELSE (portal_ephemeral.value::bigint + 1)::text
                    END,
                    expires_at = CASE
                        WHEN portal_ephemeral.expires_at <= now()
                            THEN now() + $2::interval
                        ELSE portal_ephemeral.expires_at
                    END
            RETURNING value::bigint
            """,
            key,
            ttl,
        )

        return int(count)

    async def discard(self, key: str) -> None:
        await self._pool.execute("DELETE FROM portal_ephemeral WHERE key = $1", key)

    async def sweep(self) -> int:
        """Reads already ignore expired rows, so this only reclaims space."""
        deleted = await self._pool.execute(
            "DELETE FROM portal_ephemeral WHERE expires_at <= now()"
        )

        return int(deleted.removeprefix("DELETE "))


@contextlib.asynccontextmanager
async def sweeping(store: EphemeralStore) -> AsyncIterator[None]:
    async def loop() -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            await store.sweep()

    task = asyncio.create_task(loop())

    try:
        yield
    finally:
        task.cancel()

        with contextlib.suppress(asyncio.CancelledError):
            await task
