from __future__ import annotations

import hmac
import re

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from portal.domain.errors import PermissionDenied, ProvisioningError, Reason
from portal.domain.models import WorkerIdentity
from portal.security import token_hash


if TYPE_CHECKING:
    from asyncpg import Pool


WORKER_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class PostgresWorkerRegistry:
    """Per-worker identities for portal-worker-api.

    Being on the tailnet says a request came from some worker node; this says
    which one, and lets a single compromised node be cut off immediately
    instead of waiting for a tailnet ACL change to propagate.
    """

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def issue(self, worker_id: str, credential: str, hostname: str) -> UUID:
        """Register a worker, or re-key one that already exists."""
        identity_id = await self._pool.fetchval(
            """
            INSERT INTO portal_workers (
                id,
                worker_id,
                credential_hash,
                tailscale_hostname
            )
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (worker_id) DO UPDATE
                SET credential_hash = EXCLUDED.credential_hash,
                    tailscale_hostname = EXCLUDED.tailscale_hostname,
                    revoked_at = NULL
            RETURNING id
            """,
            uuid4(),
            valid_worker_id(worker_id),
            token_hash(credential),
            hostname,
        )

        return UUID(str(identity_id))

    async def revoke(self, worker_id: str) -> bool:
        revoked = await self._pool.fetchval(
            """
            UPDATE portal_workers
               SET revoked_at = now()
             WHERE worker_id = $1
               AND revoked_at IS NULL
            RETURNING id
            """,
            worker_id,
        )

        return revoked is not None

    async def authorize(self, worker_id: str, credential: str) -> WorkerIdentity:
        row = await self._pool.fetchrow(
            """
            SELECT id, worker_id, tailscale_hostname, credential_hash
              FROM portal_workers
             WHERE worker_id = $1
               AND revoked_at IS NULL
            """,
            worker_id,
        )

        if row is None or not hmac.compare_digest(
            str(row["credential_hash"]),
            token_hash(credential),
        ):
            raise PermissionDenied(Reason.WORKER_NOT_AUTHORIZED)

        return WorkerIdentity(
            id=row["id"],
            worker_id=str(row["worker_id"]),
            tailscale_hostname=str(row["tailscale_hostname"]),
        )


def valid_worker_id(worker_id: str) -> str:
    candidate = worker_id.strip()

    if not WORKER_ID.fullmatch(candidate):
        raise ProvisioningError(Reason.WORKER_ID_INVALID)

    return candidate
