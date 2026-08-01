from __future__ import annotations

import os

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from portal.application.provisioning import ProvisioningService
from portal.application.service import PortalService
from portal.credentials.secrets import (
    DevelopmentAesGcmSecretProtector,
    SecretProtector,
    SecretRevealer,
    UnavailableSecretProtector,
)
from portal.repository.memory import InMemoryPortalRepository
from portal.repository.postgres import PostgresPortalRepository
from portal.settings import DatabaseConfigured, PortalSettings, ReadinessProbe
from portal.storage.files import FileObjectStorage
from portal.storage.memory import InMemoryObjectStorage, UnconfiguredObjectStorage
from portal.web.errors import install_error_handlers
from portal.web.routes import admin, auth, home, jobs, operations, search, teams, worker


if TYPE_CHECKING:
    from portal.repository.protocols import PortalRepository
    from portal.storage.port import ObjectStorage


def create_app(
    settings: PortalSettings | None = None,
    readiness: ReadinessProbe | None = None,
    *,
    repository: PortalRepository | None = None,
    storage: ObjectStorage | None = None,
    secret_protector: SecretProtector | None = None,
) -> FastAPI:
    """Create the server-rendered portal and inject adapters at its boundary."""
    settings = settings or PortalSettings.from_environment()
    settings.validate()
    initial_repository = repository
    initial_storage = storage

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_repository = initial_repository
        pool = None
        if active_repository is None and settings.database_dsn:
            import asyncpg

            pool = await asyncpg.create_pool(settings.database_dsn)
            active_repository = PostgresPortalRepository(pool)
        if active_repository is None:
            active_repository = InMemoryPortalRepository()
        protector = secret_protector
        if protector is None:
            protector = DevelopmentAesGcmSecretProtector.from_environment()
        if settings.is_production and protector is None:
            msg = "producción requiere PORTAL_SECRET_PROTECTION_KEY"
            raise RuntimeError(msg)
        app.state.service = PortalService(active_repository)
        app.state.repository = active_repository
        app.state.pool = pool
        app.state.provisioning = ProvisioningService(
            active_repository, protector or UnavailableSecretProtector()
        )
        app.state.secret_protector = protector
        # The worker API needs a leased queue and a protector that decrypts. A
        # deployment missing either serves the browser and refuses workers.
        app.state.worker_queue = (
            active_repository
            if isinstance(active_repository, PostgresPortalRepository)
            else None
        )
        app.state.secret_revealer = (
            protector if isinstance(protector, SecretRevealer) else None
        )
        app.state.storage = initial_storage or (
            FileObjectStorage(Path("var/objects"))
            if settings.is_production and pool is not None
            else UnconfiguredObjectStorage()
            if settings.is_production
            else InMemoryObjectStorage()
        )
        try:
            yield
        finally:
            if pool is not None:
                await pool.close()

    app = FastAPI(title="Worker", version="0.2.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.readiness = readiness or DatabaseConfigured(settings)
    app.mount(
        "/estatico",
        StaticFiles(directory=Path(__file__).with_name("static")),
        name="estatico",
    )
    if settings.is_production:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=[urlparse(settings.public_origin).hostname or "localhost"],
        )
        if not settings.tls_terminated_upstream:
            app.add_middleware(HTTPSRedirectMiddleware)
    install_error_handlers(app)
    for router in (
        operations.router,
        auth.router,
        home.router,
        jobs.router,
        search.router,
        teams.router,
        admin.router,
        worker.router,
    ):
        app.include_router(router)
    return app


def main() -> None:
    import uvicorn

    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
