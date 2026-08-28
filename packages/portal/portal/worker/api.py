from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from litestar import Litestar
from litestar.datastructures import State
from litestar.di import Provide

from portal.credentials.masterkey import MasterKeyring
from portal.credentials.secrets import EnvelopeProtector
from portal.repository.audit import PostgresAuditLog
from portal.repository.breakers import PostgresCircuitBreakers
from portal.repository.jobs import PostgresJobRepository
from portal.repository.slots import PostgresProxySlots
from portal.repository.workers import PostgresWorkerRegistry
from portal.settings import PortalSettings
from portal.storage.files import FileObjectStorage
from portal.worker.routes import (
    EXCEPTION_HANDLERS,
    handlers,
    provide_audit,
    provide_breakers,
    provide_protector,
    provide_slots,
    provide_storage,
    provide_worker,
    provide_worker_jobs,
    provide_worker_registry,
)


if TYPE_CHECKING:
    from collections.abc import Sequence


def create_worker_api(settings: PortalSettings | None = None) -> Litestar:
    resolved = settings or PortalSettings.from_environment()
    resolved.validate()

    return _build(resolved, MasterKeyring.from_file(resolved.master_key_file))


def _build(settings: PortalSettings, keyring: MasterKeyring) -> Litestar:
    @asynccontextmanager
    async def lifespan(app: Litestar) -> AsyncIterator[None]:
        import asyncpg

        pool = await asyncpg.create_pool(settings.database_dsn)

        app.state.pool = pool
        app.state.worker_queue = PostgresJobRepository(pool)
        app.state.workers = PostgresWorkerRegistry(pool)
        app.state.audit = PostgresAuditLog(pool)
        app.state.breakers = PostgresCircuitBreakers(pool)
        app.state.slots = PostgresProxySlots(pool)
        app.state.protector = EnvelopeProtector(keyring)
        app.state.storage = FileObjectStorage(settings.object_root)

        try:
            yield
        finally:
            await pool.close()

    return Litestar(
        route_handlers=[*handlers],
        dependencies={
            "worker": Provide(provide_worker),
            "worker_jobs": Provide(provide_worker_jobs, sync_to_thread=False),
            "workers": Provide(provide_worker_registry, sync_to_thread=False),
            "protector": Provide(provide_protector, sync_to_thread=False),
            "audit": Provide(provide_audit, sync_to_thread=False),
            "breakers": Provide(provide_breakers, sync_to_thread=False),
            "slots": Provide(provide_slots, sync_to_thread=False),
            "storage": Provide(provide_storage, sync_to_thread=False),
        },
        exception_handlers=EXCEPTION_HANDLERS,
        lifespan=[lifespan],
        state=State({"settings": settings}),
    )


def run(argv: Sequence[str]) -> None:
    import uvicorn

    if argv:
        raise SystemExit("portal worker-api takes no arguments")

    settings = PortalSettings.from_environment()
    settings.validate()

    # In a container, the host port binding supplies the tailnet boundary.
    # Outside one, bind directly to the Tailscale interface.
    uvicorn.run(
        create_worker_api(settings),
        host=settings.worker_api_host,
        port=settings.worker_api_port,
    )
