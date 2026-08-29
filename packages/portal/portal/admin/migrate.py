from __future__ import annotations

import argparse
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


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog="portal-admin migrate",
        description="Apply pending schema migrations.",
    )


def run(argv: Sequence[str]) -> None:
    build_parser().parse_args(argv)

    try:
        asyncio.run(migrate())
    except Exception as error:
        raise SystemExit(f"Migración no completada: {error}") from error
