from __future__ import annotations

import argparse
import asyncio

from typing import TYPE_CHECKING

import asyncpg

from portal.domain.models import AuditAction, AuditEvent
from portal.repository.audit import PostgresAuditLog
from portal.repository.workers import (
    PostgresWorkerRegistry,
    worker_database_dsn,
)
from portal.security import new_worker_credential
from portal.settings import PortalSettings


if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="portal-admin worker",
        description="Issue or revoke worker credentials.",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    issue_parser = actions.add_parser(
        "issue", help="issue worker API and database credentials"
    )
    issue_parser.add_argument("--worker-id", required=True)
    issue_parser.add_argument(
        "--tailscale-hostname",
        required=True,
        help="Node name on the tailnet.",
    )

    revoke_parser = actions.add_parser("revoke", help="revoke a worker credential")
    revoke_parser.add_argument("--worker-id", required=True)

    return parser


async def issue(
    pool: asyncpg.Pool,
    worker_id: str,
    hostname: str,
    database_dsn: str,
) -> None:
    credential = new_worker_credential()
    registry = PostgresWorkerRegistry(pool)
    identity_id = await registry.issue(
        worker_id,
        credential,
        hostname,
    )
    database_password = new_worker_credential()
    await registry.provision_login_role(worker_id, database_password)
    worker_dsn = worker_database_dsn(database_dsn, worker_id, database_password)

    await PostgresAuditLog(pool).record(
        AuditEvent(
            action=AuditAction.WORKER_ISSUED,
            target_type="worker",
            target_id=identity_id,
            metadata={"worker_id": worker_id, "tailscale_hostname": hostname},
        )
    )

    print(f"Worker: {worker_id} on {hostname}")
    print("Set PORTAL_WORKER_CREDENTIAL on that node (shown only here):")
    print(f"  {credential}")
    print("Set PORTAL_WORKER_DATABASE_DSN on that node (shown only here):")
    print(f"  {worker_dsn}")


async def revoke(pool: asyncpg.Pool, worker_id: str) -> None:
    registry = PostgresWorkerRegistry(pool)

    if not await registry.revoke(worker_id):
        msg = f"{worker_id} is not enrolled, or was already revoked"
        raise RuntimeError(msg)

    await registry.revoke_login_role(worker_id)

    await PostgresAuditLog(pool).record(
        AuditEvent(
            action=AuditAction.WORKER_REVOKED,
            target_type="worker",
            metadata={"worker_id": worker_id},
        )
    )

    print(f"Worker: {worker_id} revoked")


async def administer(args: argparse.Namespace) -> None:
    settings = PortalSettings.from_environment()
    settings.validate()

    pool = await asyncpg.create_pool(settings.database_dsn)

    try:
        if args.action == "revoke":
            await revoke(pool, args.worker_id)
            return

        await issue(
            pool,
            args.worker_id,
            args.tailscale_hostname,
            settings.database_dsn,
        )
    finally:
        await pool.close()


def run(argv: Sequence[str]) -> None:
    args = build_parser().parse_args(argv)

    try:
        asyncio.run(administer(args))
    except Exception as error:
        raise SystemExit(f"Worker administration did not complete: {error}") from error
