from __future__ import annotations

import os

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from litestar import Litestar
from litestar.config.allowed_hosts import AllowedHostsConfig
from litestar.datastructures import State
from litestar.static_files import create_static_files_router
from litestar_htmx import HTMXRequest

from portal.application.provisioning import ProvisioningService
from portal.application.service import PortalService
from portal.credentials.secrets import AesGcmSecretProtector
from portal.repository.auth import PostgresAuthRepository
from portal.repository.credentials import PostgresCredentialRepository
from portal.repository.jobs import PostgresJobRepository
from portal.repository.teams import PostgresTeamRepository
from portal.settings import PortalSettings
from portal.storage.files import FileObjectStorage
from portal.web.assets import STATIC_DIR
from portal.web.deps import DEPENDENCIES
from portal.web.errors import EXCEPTION_HANDLERS
from portal.web.headers import HTTPSRedirect, SecurityHeaders
from portal.web.routes import admin, auth, home, jobs, search, teams, worker


if TYPE_CHECKING:
    from litestar.types import Middleware


def create_app(
    settings: PortalSettings | None = None,
    protector: AesGcmSecretProtector | None = None,
) -> Litestar:
    if settings is None:
        settings = PortalSettings.from_environment()

    if protector is None:
        protector = AesGcmSecretProtector.from_environment()

    settings.validate()

    @asynccontextmanager
    async def lifespan(app: Litestar) -> AsyncIterator[None]:
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

    middleware: list[Middleware] = []

    if settings.is_production:
        hostname = urlparse(settings.public_origin).hostname
        assert hostname, "validated by PortalSettings.validate()"
        allowed_hosts = AllowedHostsConfig(allowed_hosts=[hostname])

        if not settings.tls_terminated_upstream:
            middleware.append(HTTPSRedirect)
    else:
        allowed_hosts = None

    middleware.append(SecurityHeaders)

    return Litestar(
        route_handlers=[
            create_static_files_router(path="/static", directories=[STATIC_DIR]),
            *auth.handlers,
            *home.handlers,
            jobs.router,
            search.router,
            teams.router,
            admin.router,
            worker.router,
        ],
        dependencies=DEPENDENCIES,
        exception_handlers=EXCEPTION_HANDLERS,
        request_class=HTMXRequest,
        middleware=middleware,
        allowed_hosts=allowed_hosts,
        lifespan=[lifespan],
        state=State({"settings": settings}),
    )


def main() -> None:
    import uvicorn

    uvicorn.run(
        create_app(),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
