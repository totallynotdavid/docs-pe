from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Form, HTTPException, Request

from portal.application.provisioning import ProvisioningService
from portal.application.service import PortalService
from portal.domain.models import BrowserSession
from portal.settings import PortalSettings, ReadinessProbe
from portal.storage.port import ObjectStorage
from portal.web.errors import LoginRequired
from portal.web.security import same_origin


def _readiness(request: Request) -> ReadinessProbe:
    probe: ReadinessProbe = request.app.state.readiness
    return probe


def _settings(request: Request) -> PortalSettings:
    settings: PortalSettings = request.app.state.settings
    return settings


def _service(request: Request) -> PortalService:
    service: PortalService = request.app.state.service
    return service


def _provisioning(request: Request) -> ProvisioningService:
    provisioning: ProvisioningService = request.app.state.provisioning
    return provisioning


def _storage(request: Request) -> ObjectStorage:
    storage: ObjectStorage = request.app.state.storage
    return storage


Readiness = Annotated[ReadinessProbe, Depends(_readiness)]
Settings = Annotated[PortalSettings, Depends(_settings)]
Service = Annotated[PortalService, Depends(_service)]
Provisioning = Annotated[ProvisioningService, Depends(_provisioning)]
Storage = Annotated[ObjectStorage, Depends(_storage)]


def require_same_origin(request: Request, settings: Settings) -> None:
    """Reject a cross-site form post before any handler runs."""
    if not same_origin(
        origin=request.headers.get("origin"),
        referer=request.headers.get("referer"),
        trusted_origin=settings.public_origin,
    ):
        raise HTTPException(status_code=403, detail="origen no autorizado")


RequireSameOrigin = Depends(require_same_origin)


async def _optional_session(
    request: Request, service: Service, settings: Settings
) -> BrowserSession | None:
    return await service.browser_session(request.cookies.get(settings.session_cookie))


OptionalSession = Annotated[BrowserSession | None, Depends(_optional_session)]


async def _page_session(session: OptionalSession) -> BrowserSession:
    if session is None:
        raise LoginRequired
    return session


async def _api_session(session: OptionalSession) -> BrowserSession:
    if session is None:
        raise HTTPException(status_code=401, detail="autenticación requerida")
    return session


async def _verified_session(
    request: Request,
    service: Service,
    settings: Settings,
    csrf_token: Annotated[str, Form()],
) -> BrowserSession:
    """Authorize a state-changing form post before its route body runs."""
    require_same_origin(request, settings)
    return await service.verify_browser_csrf(
        request.cookies.get(settings.session_cookie), csrf_token
    )


# A browser navigation without a session is sent to the login page; an htmx or
# API caller is told so with a status code. Mutations answer 403 either way.
PageSession = Annotated[BrowserSession, Depends(_page_session)]
ApiSession = Annotated[BrowserSession, Depends(_api_session)]
VerifiedSession = Annotated[BrowserSession, Depends(_verified_session)]

# For the one mutation that acts on the cookie alone and never names its actor.
RequireVerifiedSession = Depends(_verified_session)
