from __future__ import annotations

from typing import TYPE_CHECKING

from litestar.di import NamedDependency, Provide
from litestar.exceptions import NotAuthorizedException, PermissionDeniedException

from portal.application.login import LoginService
from portal.application.provisioning import ProvisioningService
from portal.application.service import PortalService
from portal.application.sessions import BrowserSessions
from portal.application.throttle import MutationThrottle
from portal.domain.models import BrowserSession, TeamRole
from portal.security import same_origin
from portal.settings import PortalSettings
from portal.storage.port import ObjectStorage
from portal.web.errors import LoginRequired
from portal.web.trace import client_trace


if TYPE_CHECKING:
    from collections.abc import Sequence

    from litestar import Request
    from litestar.connection import ASGIConnection
    from litestar.datastructures import State
    from litestar.handlers.base import BaseRouteHandler

    from portal.domain.models import PortalUser, Team


def is_search_only(user: PortalUser, teams: Sequence[Team]) -> bool:
    """True when this session has nothing to manage: not a site admin, and
    not a team_leader anywhere. Drives the minimal, sidebar-less shell:
    someone whose entire job is searching results shouldn't see a nav built
    for managing teams and jobs they can't touch."""
    if user.is_site_admin:
        return False

    return not any(team.role is TeamRole.TEAM_LEADER for team in teams)


def provide_settings(state: State) -> PortalSettings:
    return state.settings


def provide_service(state: State) -> PortalService:
    return state.service


def provide_provisioning(state: State) -> ProvisioningService:
    return state.provisioning


def provide_login(state: State) -> LoginService:
    return state.login


def provide_sessions(state: State) -> BrowserSessions:
    return state.sessions


def provide_storage(state: State) -> ObjectStorage:
    return state.storage


def _deny_unless_same_origin(
    connection: ASGIConnection,
    settings: PortalSettings,
) -> None:
    if not same_origin(
        origin=connection.headers.get("origin"),
        referer=connection.headers.get("referer"),
        trusted_origin=settings.public_origin,
        sec_fetch_site=connection.headers.get("sec-fetch-site"),
    ):
        raise PermissionDeniedException(detail="origen no autorizado")


def require_same_origin(
    connection: ASGIConnection,
    _: BaseRouteHandler,
) -> None:
    _deny_unless_same_origin(connection, connection.app.state.settings)


async def provide_optional_session(
    request: Request,
    sessions: NamedDependency[BrowserSessions],
    settings: NamedDependency[PortalSettings],
) -> BrowserSession | None:
    session = await sessions.load(request.cookies.get(settings.session_cookie))

    # Stashed so the PermissionDenied handler can attribute an audit entry
    # without loading the session a second time.
    if session is not None:
        request.state.actor_id = session.user.id

    return session


async def provide_page_session(
    optional_session: NamedDependency[BrowserSession | None],
) -> BrowserSession:
    if optional_session is None:
        raise LoginRequired

    return optional_session


async def provide_api_session(
    optional_session: NamedDependency[BrowserSession | None],
) -> BrowserSession:
    if optional_session is None:
        raise NotAuthorizedException(detail="autenticación requerida")

    return optional_session


async def require_verified_session(
    request: Request,
    settings: PortalSettings,
    csrf_token: str,
) -> BrowserSession:
    """The gate every state-changing request goes through.

    Same-origin, then the synchronizer token, then the per-actor cap. The cap
    comes last because it is keyed by the actor, which only the verified
    session establishes.

    https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
    """
    _deny_unless_same_origin(request, settings)

    sessions: BrowserSessions = request.app.state.sessions
    throttle: MutationThrottle = request.app.state.mutation_throttle

    session = await sessions.verify_csrf(
        request.cookies.get(settings.session_cookie),
        csrf_token,
    )

    if not await throttle.admit(session.user.id, _route_class(request)):
        raise PermissionDeniedException(detail="demasiadas solicitudes seguidas")

    return session


def _route_class(request: Request) -> str:
    """Group routes by their first path segment, so one busy form cannot
    exhaust the budget another form needs."""
    return request.scope["path"].strip("/").split("/")[0] or "root"


DEPENDENCIES = {
    "settings": Provide(provide_settings, sync_to_thread=False),
    "service": Provide(provide_service, sync_to_thread=False),
    "provisioning": Provide(provide_provisioning, sync_to_thread=False),
    "login": Provide(provide_login, sync_to_thread=False),
    "sessions": Provide(provide_sessions, sync_to_thread=False),
    "storage": Provide(provide_storage, sync_to_thread=False),
    "trace": Provide(client_trace, sync_to_thread=False),
    "optional_session": Provide(provide_optional_session),
    "page_session": Provide(provide_page_session),
    "api_session": Provide(provide_api_session),
}
