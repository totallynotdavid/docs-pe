from __future__ import annotations

from fastapi import APIRouter, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from portal.application.provisioning import ProvisioningService
from portal.domain.errors import PortalError
from portal.domain.models import BrowserSession
from portal.web.deps import PageSession, Provisioning, VerifiedSession
from portal.web.render import render


router = APIRouter(prefix="/administracion")


@router.get("", response_class=HTMLResponse)
async def administration_home(
    session: PageSession,
    provisioning: Provisioning,
) -> Response:
    await provisioning.installation_status(session.user.id)

    return RedirectResponse("/administracion/equipos", status_code=303)


async def _users_context(
    session: BrowserSession,
    provisioning: ProvisioningService,
    *,
    error: str = "",
) -> dict[str, object]:
    return {
        "user": session.user,
        "csrf_token": session.csrf_token,
        "users": await provisioning.users(session.user.id),
        "error": error,
    }


@router.get("/usuarios", response_class=HTMLResponse)
async def administration_users_get(
    session: PageSession,
    provisioning: Provisioning,
) -> Response:
    context = await _users_context(session, provisioning)

    return render("SiteUsers", **context)


@router.post("/usuarios", response_class=HTMLResponse)
async def administration_users_post(
    session: VerifiedSession,
    provisioning: Provisioning,
    email: str = Form(),
    password: str = Form(),
) -> Response:
    try:
        await provisioning.create_user(
            session.user.id,
            email=email,
            password=password,
        )
    except (PortalError, ValueError) as error:
        context = await _users_context(
            session,
            provisioning,
            error=str(error),
        )

        return render("SiteUsers", **context)

    return RedirectResponse("/administracion/usuarios", status_code=303)


async def _teams_context(
    session: BrowserSession,
    provisioning: ProvisioningService,
    *,
    error: str = "",
) -> dict[str, object]:
    return {
        "user": session.user,
        "csrf_token": session.csrf_token,
        "teams": await provisioning.teams(session.user.id),
        "users": await provisioning.users(session.user.id),
        "status": await provisioning.installation_status(session.user.id),
        "error": error,
    }


@router.get("/equipos", response_class=HTMLResponse)
async def administration_teams_get(
    session: PageSession,
    provisioning: Provisioning,
) -> Response:
    context = await _teams_context(session, provisioning)

    return render("SiteTeams", **context)


@router.post("/equipos", response_class=HTMLResponse)
async def administration_teams_post(
    session: VerifiedSession,
    provisioning: Provisioning,
    name: str = Form(),
    slug: str = Form(),
    leader_email: str = Form(),
) -> Response:
    try:
        team = await provisioning.create_team(
            session.user.id,
            name=name,
            slug=slug,
            leader_email=leader_email,
        )
    except (PortalError, ValueError) as error:
        context = await _teams_context(
            session,
            provisioning,
            error=str(error),
        )

        return render("SiteTeams", **context)

    return RedirectResponse(f"/equipos/{team.id}/ajustes", status_code=303)
