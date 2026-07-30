from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from asyncpg import Pool


async def apply_migrations(pool: Pool) -> None:
    """Apply ordered SQL migrations exactly once, using PostgreSQL as the ledger."""
    async with pool.acquire() as connection, connection.transaction():
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS portal_schema_migrations (
                version text PRIMARY KEY,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        for path in sorted(Path(__file__).with_name("migrations").glob("*.sql")):
            applied = await connection.fetchval(
                "SELECT 1 FROM portal_schema_migrations WHERE version = $1", path.name
            )
            if applied is None:
                await connection.execute(path.read_text(encoding="utf-8"))
                await connection.execute(
                    "INSERT INTO portal_schema_migrations (version) VALUES ($1)",
                    path.name,
                )
