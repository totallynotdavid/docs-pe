from __future__ import annotations

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from portal.domain.errors import PortalError
from portal.web.deps import PageSession, Provisioning, Service, VerifiedSession
from portal.web.render import render, render_hx


router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    session: PageSession, service: Service, provisioning: Provisioning
) -> Response:
    if session.user.is_site_admin:
        status = await provisioning.installation_status(session.user.id)
        if status.can_create_first_team:
            return RedirectResponse("/inicio", status_code=303)
    return render(
        "dashboard.html",
        user=session.user,
        csrf_token=session.csrf_token,
        teams=await service.teams(session.user.id),
    )


@router.get("/inicio", response_class=HTMLResponse)
async def first_team_get(session: PageSession, provisioning: Provisioning) -> Response:
    status = await provisioning.installation_status(session.user.id)
    if not status.can_create_first_team:
        return RedirectResponse("/", status_code=303)
    return render(
        "first_team.html",
        user=session.user,
        csrf_token=session.csrf_token,
        error="",
        setup=True,
    )


@router.post("/inicio", response_class=HTMLResponse)
async def first_team_post(
    session: VerifiedSession,
    provisioning: Provisioning,
    name: str = Form(),
    slug: str = Form(),
) -> Response:
    try:
        team = await provisioning.create_first_team(
            session.user.id, name=name, slug=slug
        )
    except (PortalError, ValueError) as error:
        return render(
            "first_team.html",
            user=session.user,
            csrf_token=session.csrf_token,
            error=str(error),
            setup=True,
        )
    return RedirectResponse(f"/equipos/{team.id}/ajustes/proxy", status_code=303)


@router.get("/notificaciones", response_class=HTMLResponse)
async def notifications(
    request: Request, session: PageSession, service: Service
) -> Response:
    return render_hx(
        request,
        "notifications.html",
        "fragments/notifications.html",
        user=session.user,
        csrf_token=session.csrf_token,
        notifications=await service.notifications(session.user.id),
    )
