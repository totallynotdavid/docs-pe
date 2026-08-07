from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from litestar import Request, Response, get, post
from litestar.di import NamedDependency
from litestar.enums import RequestEncodingType
from litestar.exceptions import PermissionDeniedException
from litestar.params import Body, FromQuery
from litestar.response import Redirect

from portal.application.service import PortalService
from portal.domain.models import BrowserSession
from portal.settings import PortalSettings
from portal.web.deps import require_same_origin, require_verified_session
from portal.web.render import render


@dataclass
class LoginForm:
    email: str
    password: str
    csrf_token: str


@get("/login")
async def login_get(
    service: NamedDependency[PortalService],
    optional_session: NamedDependency[BrowserSession | None],
    error: FromQuery[str] = "",
) -> Response:
    if optional_session is not None:
        return Redirect("/", status_code=303)

    csrf_token = await service.issue_login_csrf()
    return render("Login", user=None, csrf_token=csrf_token, error=error)


@post("/login", guards=[require_same_origin], status_code=200)
async def login_post(
    request: Request,
    service: NamedDependency[PortalService],
    settings: NamedDependency[PortalSettings],
    data: Annotated[LoginForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    if not await service.consume_login_csrf(data.csrf_token):
        raise PermissionDeniedException(detail="verificación CSRF no válida")

    login = await service.login(
        data.email,
        data.password,
        request.client.host if request.client else "desconocido",
    )
    if login is None:
        return Redirect("/login?error=1", status_code=303)

    _, token = login

    await service.destroy_session(request.cookies.get(settings.session_cookie))

    response = Redirect("/", status_code=303)
    response.set_cookie(
        settings.session_cookie,
        token,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@dataclass
class LogoutForm:
    csrf_token: str


@post("/logout", status_code=200)
async def logout_post(
    request: Request,
    service: NamedDependency[PortalService],
    settings: NamedDependency[PortalSettings],
    data: Annotated[LogoutForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    await require_verified_session(request, service, settings, data.csrf_token)

    await service.destroy_session(request.cookies.get(settings.session_cookie))

    response = Redirect("/login", status_code=303)
    response.delete_cookie(settings.session_cookie, path="/")
    return response


handlers = (login_get, login_post, logout_post)
