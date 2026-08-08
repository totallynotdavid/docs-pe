from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from portal.migrations import apply_migrations
from portal.settings import PortalSettings


if TYPE_CHECKING:
    from collections.abc import Sequence


async def migrate() -> None:
    settings = PortalSettings.from_environment()

    if not settings.database_dsn:
        msg = "PORTAL_DATABASE_DSN es obligatorio para migrar"
        raise RuntimeError(msg)

    import asyncpg

    pool = await asyncpg.create_pool(settings.database_dsn)

    try:
        await apply_migrations(pool)
    finally:
        await pool.close()


def run(argv: Sequence[str]) -> None:
    if argv:
        raise SystemExit("portal migrate takes no arguments")

    try:
        asyncio.run(migrate())
    except Exception as error:
        raise SystemExit(f"Migración no completada: {error}") from error
