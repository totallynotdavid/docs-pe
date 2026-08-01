from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from portal.web.deps import (
    OptionalSession,
    RequireSameOrigin,
    RequireVerifiedSession,
    Service,
    Settings,
)
from portal.web.render import render


router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_get(
    service: Service, session: OptionalSession, error: str = ""
) -> Response:
    if session is not None:
        return RedirectResponse("/", status_code=303)
    csrf_token = await service.issue_login_csrf()
    return render("login.html", user=None, csrf_token=csrf_token, error=error)


@router.post("/login", dependencies=[RequireSameOrigin])
async def login_post(
    request: Request,
    service: Service,
    settings: Settings,
    email: str = Form(),
    password: str = Form(),
    csrf_token: str = Form(),
) -> Response:
    # The login form carries a single-use token of its own: there is no session
    # yet to hold one.
    if not await service.consume_login_csrf(csrf_token):
        raise HTTPException(status_code=403, detail="verificación CSRF no válida")
    client_ip = request.client.host if request.client else "desconocido"
    login = await service.login(email, password, client_ip)
    if login is None:
        return RedirectResponse("/login?error=1", status_code=303)
    _, token = login
    await service.destroy_session(request.cookies.get(settings.session_cookie))
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        settings.session_cookie,
        token,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout", dependencies=[RequireVerifiedSession])
async def logout_post(
    request: Request, service: Service, settings: Settings
) -> Response:
    await service.destroy_session(request.cookies.get(settings.session_cookie))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(settings.session_cookie, path="/")
    return response
