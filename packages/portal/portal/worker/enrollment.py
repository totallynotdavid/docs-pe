from __future__ import annotations

import argparse
import asyncio

from typing import TYPE_CHECKING

import asyncpg

from portal.domain.models import AuditAction, AuditEvent
from portal.repository.audit import PostgresAuditLog
from portal.repository.workers import PostgresWorkerRegistry
from portal.security import new_worker_credential
from portal.settings import PortalSettings


if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="portal enroll-worker",
        description="Issue or revoke a worker's credential for portal-worker-api.",
    )
    parser.add_argument("--worker-id", required=True)
    parser.add_argument(
        "--tailscale-hostname",
        default="",
        help="Node name on the tailnet. Required when issuing.",
    )
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="Revoke instead of issuing. Takes effect on the next request.",
    )
    return parser


async def issue(pool: asyncpg.Pool, worker_id: str, hostname: str) -> None:
    credential = new_worker_credential()
    identity_id = await PostgresWorkerRegistry(pool).issue(
        worker_id,
        credential,
        hostname,
    )

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


async def revoke(pool: asyncpg.Pool, worker_id: str) -> None:
    if not await PostgresWorkerRegistry(pool).revoke(worker_id):
        msg = f"{worker_id} is not enrolled, or was already revoked"
        raise RuntimeError(msg)

    await PostgresAuditLog(pool).record(
        AuditEvent(
            action=AuditAction.WORKER_REVOKED,
            target_type="worker",
            metadata={"worker_id": worker_id},
        )
    )

    print(f"Worker: {worker_id} revoked")


async def enroll(args: argparse.Namespace) -> None:
    settings = PortalSettings.from_environment()
    settings.validate()

    pool = await asyncpg.create_pool(settings.database_dsn)

    try:
        if args.revoke:
            await revoke(pool, args.worker_id)
            return

        if not args.tailscale_hostname:
            msg = "--tailscale-hostname is required when issuing"
            raise RuntimeError(msg)

        await issue(pool, args.worker_id, args.tailscale_hostname)
    finally:
        await pool.close()


def run(argv: Sequence[str]) -> None:
    args = build_parser().parse_args(argv)

    try:
        asyncio.run(enroll(args))
    except Exception as error:
        raise SystemExit(f"Worker enrollment did not complete: {error}") from error
