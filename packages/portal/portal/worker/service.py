from __future__ import annotations

from uuid import UUID

from portal.domain.models import STABLE_SOURCES, ClaimedWork
from portal.repository.protocols import WorkerQueue


class PortalWorker:
    """Minimal process boundary; fetch adapters are called by later worker wiring."""

    def __init__(
        self, queue: WorkerQueue, worker_id: str, sources: tuple[str, ...]
    ) -> None:
        if not sources:
            msg = "el trabajador debe declarar al menos una fuente"
            raise ValueError(msg)
        unsupported = sorted(set(sources).difference(STABLE_SOURCES))
        if unsupported:
            msg = f"fuentes no habilitadas para el trabajador: {', '.join(unsupported)}"
            raise ValueError(msg)
        self._queue = queue
        self._worker_id = worker_id
        self._sources = sources

    async def claim(self) -> ClaimedWork | None:
        """Claiming is PostgreSQL-fenced; lookup and object storage happen afterward."""
        return await self._queue.claim(self._worker_id, self._sources)

    async def publish(self, work: ClaimedWork, result_object_id: UUID) -> bool:
        """A cancellation or expired lease makes this fenced write a safe no-op."""
        return await self._queue.publish(
            work.item_id, self._worker_id, work.lease_fence, result_object_id
        )


def main() -> None:
    msg = "configure a PostgreSQL worker adapter before starting osiptel-portal-worker"
    raise SystemExit(msg)
