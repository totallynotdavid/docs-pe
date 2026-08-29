#!/usr/bin/env python
"""One-time migration of portal's local-disk objects into S3-compatible storage.

Pages portal_object_references, reads each object from the local directory
FileObjectStorage used (files named by the reference's own id), and PUTs it
into the bucket under the new container/object_key scheme. Safe to re-run:
an existing key with matching content is skipped, so an interrupted run just
picks back up.

Self-contained (asyncpg + aioboto3 only, no `portal` import) so it can run in
a throwaway container that never had the portal package installed, since
Postgres has no host-exposed port on the box this reads from.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

import aioboto3
import asyncpg

from botocore.config import Config
from botocore.exceptions import ClientError


BATCH_SIZE = 500
CONCURRENCY = 64

Outcome = Literal["migrated", "skipped_existing", "missing_local", "mismatched"]


@dataclass
class Counters:
    migrated: int = 0
    skipped_existing: int = 0
    missing_local: int = 0
    mismatched: int = 0

    def add(self, outcome: Outcome) -> None:
        setattr(self, outcome, getattr(self, outcome) + 1)

    def __str__(self) -> str:
        return (
            f"migrated={self.migrated} skipped={self.skipped_existing} "
            f"missing={self.missing_local} mismatched={self.mismatched}"
        )


async def _migrate_one(
    client: object, args: argparse.Namespace, row: object
) -> Outcome:
    key = f"{row['container']}/{row['object_key']}"  # type: ignore[index]
    local_path = Path(args.object_root) / str(row["id"])  # type: ignore[index]

    if not local_path.exists():
        print(f"missing local file: {row['id']}", file=sys.stderr)  # type: ignore[index]
        return "missing_local"

    content = local_path.read_bytes()

    if sha256(content).hexdigest() != row["sha256"]:  # type: ignore[index]
        print(f"sha256 mismatch: {row['id']}", file=sys.stderr)  # type: ignore[index]
        return "mismatched"

    try:
        existing = await client.get_object(Bucket=args.bucket, Key=key)  # type: ignore[attr-defined]
    except ClientError as error:
        if error.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
            raise
    else:
        body = await existing["Body"].read()
        if body == content:
            return "skipped_existing"
        print(f"bucket key already differs: {key}", file=sys.stderr)
        return "mismatched"

    await client.put_object(Bucket=args.bucket, Key=key, Body=content)  # type: ignore[attr-defined]
    return "migrated"


async def run(args: argparse.Namespace) -> None:
    pool = await asyncpg.create_pool(
        args.database_dsn, min_size=1, max_size=args.concurrency
    )
    session = aioboto3.Session()
    client_kwargs = {
        "endpoint_url": args.endpoint_url,
        "aws_access_key_id": args.access_key,
        "aws_secret_access_key": args.secret_key,
        "region_name": args.region,
        "config": Config(max_pool_connections=args.concurrency),
    }

    counters = Counters()
    last_id = None
    semaphore = asyncio.Semaphore(args.concurrency)

    async def bounded(client: object, row: object) -> Outcome:
        async with semaphore:
            return await _migrate_one(client, args, row)

    try:
        async with session.client("s3", **client_kwargs) as client:
            while True:
                rows = await pool.fetch(
                    """
                    SELECT id, container, object_key, sha256
                      FROM portal_object_references
                     WHERE ($1::uuid IS NULL OR id > $1)
                     ORDER BY id
                     LIMIT $2
                    """,
                    last_id,
                    args.batch_size,
                )

                if not rows:
                    break

                last_id = rows[-1]["id"]
                outcomes = await asyncio.gather(*(bounded(client, row) for row in rows))

                for outcome in outcomes:
                    counters.add(outcome)

                print(f"progress: {counters}", flush=True)
    finally:
        await pool.close()

    print(f"done: {counters}")

    if counters.missing_local or counters.mismatched:
        msg = (
            "backfill finished with problems -- do not cut over until these "
            "are understood (see stderr for the affected ids)"
        )
        raise SystemExit(msg)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-dsn", required=True)
    parser.add_argument(
        "--object-root",
        required=True,
        help="Local directory FileObjectStorage wrote to (files named by id).",
    )
    parser.add_argument("--endpoint-url", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--access-key", required=True)
    parser.add_argument("--secret-key", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
