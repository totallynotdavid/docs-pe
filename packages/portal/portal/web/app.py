from __future__ import annotations

import os

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from portal.application.provisioning import ProvisioningService
from portal.application.service import PortalService
from portal.credentials.secrets import AesGcmSecretProtector
from portal.repository.postgres import PostgresPortalRepository
from portal.settings import DatabaseConfigured, PortalSettings, ReadinessProbe
from portal.storage.files import FileObjectStorage
from portal.web.errors import install_error_handlers
from portal.web.headers import SecurityHeaders
from portal.web.routes import (
    admin,
    assets,
    auth,
    home,
    jobs,
    operations,
    search,
    teams,
    worker,
)


def create_app(
    settings: PortalSettings | None = None, readiness: ReadinessProbe | None = None
) -> FastAPI:
    """Create the server-rendered portal.

    One repository and one object store. The portal is PostgreSQL-only, so a missing
    DSN fails at startup.
    """
    settings = settings or PortalSettings.from_environment()
    settings.validate()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        import asyncpg

        pool = await asyncpg.create_pool(settings.database_dsn)
        repository = PostgresPortalRepository(pool)
        protector = AesGcmSecretProtector.from_environment()
        app.state.pool = pool
        app.state.repository = repository
        app.state.worker_queue = repository
        app.state.service = PortalService(repository)
        app.state.provisioning = ProvisioningService(repository, protector)
        app.state.secret_protector = protector
        app.state.storage = FileObjectStorage(settings.object_root)
        try:
            yield
        finally:
            await pool.close()

    app = FastAPI(title="Worker", version="0.2.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.readiness = readiness or DatabaseConfigured(settings)
    # Component stylesheets live under the static prefix but are served by a route,
    # so they have to be matched before the mount that would otherwise swallow them.
    app.include_router(assets.router)
    app.mount(
        "/estatico",
        StaticFiles(directory=Path(__file__).with_name("static")),
        name="estatico",
    )
    app.add_middleware(SecurityHeaders)
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
