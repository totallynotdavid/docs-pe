from __future__ import annotations

import os

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from litestar import Litestar
from litestar.config.allowed_hosts import AllowedHostsConfig
from litestar.datastructures import State
from litestar.static_files import create_static_files_router
from litestar_htmx import HTMXRequest

from portal.application.login import LoginService
from portal.application.provisioning import ProvisioningService
from portal.application.service import PortalService
from portal.application.sessions import BrowserSessions, OneTimeTokens
from portal.application.throttle import LoginThrottle, MutationThrottle
from portal.credentials.masterkey import MasterKeyring
from portal.credentials.secrets import EnvelopeProtector
from portal.ephemeral import EphemeralStore, sweeping
from portal.repository.audit import PostgresAuditLog
from portal.repository.auth import PostgresAuthRepository
from portal.repository.credentials import PostgresCredentialRepository
from portal.repository.jobs import PostgresJobRepository
from portal.repository.teams import PostgresTeamRepository
from portal.settings import PortalSettings
from portal.storage.files import FileObjectStorage
from portal.turnstile import open_human_check
from portal.web.assets import STATIC_DIR
from portal.web.deps import DEPENDENCIES
from portal.web.errors import AFTER_EXCEPTION, EXCEPTION_HANDLERS
from portal.web.headers import HTTPSRedirect, SecurityHeaders
from portal.web.routes import admin, auth, home, jobs, search, stepup, teams


if TYPE_CHECKING:
    from collections.abc import Sequence

    from litestar.types import Middleware


def create_web_app(settings: PortalSettings | None = None) -> Litestar:
    """The public listener. Worker traffic is served by portal-worker-api."""
    resolved = settings or PortalSettings.from_environment()
    resolved.validate()

    # Loaded here rather than on first use: a key file that is missing or
    # malformed should stop the process at startup, not the first login.
    return _build(resolved, MasterKeyring.from_file(resolved.master_key_file))


def _build(settings: PortalSettings, keyring: MasterKeyring) -> Litestar:
    @asynccontextmanager
    async def lifespan(app: Litestar) -> AsyncIterator[None]:
        import asyncpg

        pool = await asyncpg.create_pool(settings.database_dsn)

        store = EphemeralStore(pool)
        auth_repo = PostgresAuthRepository(pool)
        audit = PostgresAuditLog(pool)
        protector = EnvelopeProtector(keyring)
        sessions = BrowserSessions(store, auth_repo)
        human_check = open_human_check(settings)

        app.state.pool = pool
        app.state.sessions = sessions
        app.state.mutation_throttle = MutationThrottle(store)
        app.state.login = LoginService(
            auth_repo,
            sessions,
            OneTimeTokens(store),
            LoginThrottle(store),
            human_check,
            protector,
            audit,
        )
        app.state.service = PortalService(
            PostgresTeamRepository(pool),
            PostgresCredentialRepository(pool),
            PostgresJobRepository(pool),
        )
        app.state.provisioning = ProvisioningService(
            auth_repo,
            PostgresTeamRepository(pool),
            PostgresCredentialRepository(pool),
            protector,
            audit,
            settings.hostname,
        )
        app.state.audit = audit
        app.state.storage = FileObjectStorage(settings.object_root)

        try:
            async with sweeping(store):
                yield
        finally:
            await human_check.aclose()
            await pool.close()

    middleware: list[Middleware] = [SecurityHeaders]

    # Not gated on PORTAL_ENVIRONMENT: a deployment that declares an https
    # origin gets the redirect, and one that terminates TLS upstream would
    # otherwise loop forever redirecting traffic it already received over https.
    if settings.serves_https and not settings.tls_terminated_upstream:
        middleware.insert(0, HTTPSRedirect)

    return Litestar(
        route_handlers=[
            create_static_files_router(path="/static", directories=[STATIC_DIR]),
            *auth.handlers,
            *home.handlers,
            *stepup.handlers,
            jobs.router,
            search.router,
            teams.router,
            admin.router,
        ],
        dependencies=DEPENDENCIES,
        exception_handlers=EXCEPTION_HANDLERS,
        after_exception=AFTER_EXCEPTION,
        request_class=HTMXRequest,
        middleware=middleware,
        allowed_hosts=AllowedHostsConfig(allowed_hosts=list(settings.allowed_hosts)),
        lifespan=[lifespan],
        state=State({"settings": settings}),
    )


def run(argv: Sequence[str]) -> None:
    import uvicorn

    if argv:
        raise SystemExit("portal web takes no arguments")

    settings = PortalSettings.from_environment()
    settings.validate()

    # No proxy_headers: the client address comes from CF-Connecting-IP, which
    # Cloudflare sets after stripping any client-supplied copy and which reaches
    # the app through the tunnel. Trusting X-Forwarded-For instead would mean
    # trusting a header that arrives with every request, and the hop that would
    # have to be allowlisted is the local reverse proxy, not the edge.
    uvicorn.run(
        create_web_app(settings),
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
    )
