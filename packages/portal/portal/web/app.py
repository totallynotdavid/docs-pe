from __future__ import annotations

import os

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from portal.application.provisioning import ProvisioningService
from portal.application.service import PortalService
from portal.credentials.secrets import AesGcmSecretProtector
from portal.repository.auth import PostgresAuthRepository
from portal.repository.credentials import PostgresCredentialRepository
from portal.repository.jobs import PostgresJobRepository
from portal.repository.teams import PostgresTeamRepository
from portal.settings import PortalSettings
from portal.storage.files import FileObjectStorage
from portal.web.errors import install_error_handlers
from portal.web.headers import SecurityHeaders
from portal.web.routes import (
    admin,
    assets,
    auth,
    home,
    jobs,
    search,
    teams,
    worker,
)


def create_app(
    settings: PortalSettings | None = None,
    protector: AesGcmSecretProtector | None = None,
) -> FastAPI:
    if settings is None:
        settings = PortalSettings.from_environment()

    if protector is None:
        protector = AesGcmSecretProtector.from_environment()

    settings.validate()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        import asyncpg

        pool = await asyncpg.create_pool(settings.database_dsn)

        auth_repository = PostgresAuthRepository(pool)
        team_repository = PostgresTeamRepository(pool)
        credential_repository = PostgresCredentialRepository(pool)
        job_repository = PostgresJobRepository(pool)

        app.state.pool = pool
        app.state.worker_queue = job_repository
        app.state.service = PortalService(
            auth_repository,
            team_repository,
            credential_repository,
            job_repository,
        )
        app.state.provisioning = ProvisioningService(
            auth_repository,
            team_repository,
            credential_repository,
            protector,
        )
        app.state.secret_protector = protector
        app.state.storage = FileObjectStorage(settings.object_root)

        try:
            yield
        finally:
            await pool.close()

    app = FastAPI(
        title="Worker",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.settings = settings

    # Register component assets before the static mount can match them.
    app.include_router(assets.router)
    app.mount(
        "/estatico",
        StaticFiles(directory=Path(__file__).with_name("static")),
        name="estatico",
    )

    app.add_middleware(SecurityHeaders)

    install_error_handlers(app)

    for router in (
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
    )
