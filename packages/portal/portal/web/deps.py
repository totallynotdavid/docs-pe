from __future__ import annotations

from typing import TYPE_CHECKING

from litestar.di import NamedDependency, Provide
from litestar.exceptions import NotAuthorizedException, PermissionDeniedException

from portal.application.provisioning import ProvisioningService
from portal.application.service import PortalService
from portal.domain.models import BrowserSession
from portal.security import same_origin
from portal.settings import PortalSettings
from portal.storage.port import ObjectStorage
from portal.web.errors import LoginRequired


if TYPE_CHECKING:
    from litestar import Request
    from litestar.connection import ASGIConnection
    from litestar.datastructures import State
    from litestar.handlers.base import BaseRouteHandler


def provide_settings(state: State) -> PortalSettings:
    return state.settings


def provide_service(state: State) -> PortalService:
    return state.service


def provide_provisioning(state: State) -> ProvisioningService:
    return state.provisioning


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
    ):
        raise PermissionDeniedException(detail="origen no autorizado")


def require_same_origin(
    connection: ASGIConnection,
    _: BaseRouteHandler,
) -> None:
    _deny_unless_same_origin(connection, connection.app.state.settings)


async def provide_optional_session(
    request: Request,
    service: NamedDependency[PortalService],
    settings: NamedDependency[PortalSettings],
) -> BrowserSession | None:
    return await service.browser_session(request.cookies.get(settings.session_cookie))


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
    service: PortalService,
    settings: PortalSettings,
    csrf_token: str,
) -> BrowserSession:
    _deny_unless_same_origin(request, settings)

    return await service.verify_browser_csrf(
        request.cookies.get(settings.session_cookie),
        csrf_token,
    )


DEPENDENCIES = {
    "settings": Provide(provide_settings, sync_to_thread=False),
    "service": Provide(provide_service, sync_to_thread=False),
    "provisioning": Provide(provide_provisioning, sync_to_thread=False),
    "storage": Provide(provide_storage, sync_to_thread=False),
    "optional_session": Provide(provide_optional_session),
    "page_session": Provide(provide_page_session),
    "api_session": Provide(provide_api_session),
}
