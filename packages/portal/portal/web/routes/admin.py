from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from litestar import Response, Router, get, post
from litestar.di import NamedDependency
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect
from litestar_htmx import HTMXRequest

from portal.application.provisioning import ProvisioningService
from portal.application.service import PortalService
from portal.domain.errors import PortalError
from portal.domain.models import BrowserSession
from portal.settings import PortalSettings
from portal.web.deps import require_verified_session
from portal.web.render import render


@get("")
async def admin_home(
    page_session: NamedDependency[BrowserSession],
    provisioning: NamedDependency[ProvisioningService],
) -> Response:
    await provisioning.require_site_admin(page_session.user.id)
    return Redirect("/admin/teams", status_code=303)


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


@get("/users")
async def admin_users_get(
    page_session: NamedDependency[BrowserSession],
    provisioning: NamedDependency[ProvisioningService],
) -> Response:
    context = await _users_context(page_session, provisioning)
    return render("SiteUsers", **context)


@dataclass
class NewUserForm:
    email: str
    password: str
    csrf_token: str


@post("/users", status_code=200)
async def admin_users_post(
    request: HTMXRequest,
    service: NamedDependency[PortalService],
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    data: Annotated[NewUserForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    session = await require_verified_session(
        request,
        service,
        settings,
        data.csrf_token,
    )

    try:
        await provisioning.create_user(
            session.user.id,
            email=data.email,
            password=data.password,
        )
    except (PortalError, ValueError) as error:
        context = await _users_context(
            session,
            provisioning,
            error=str(error),
        )
        return render("SiteUsers", **context)

    return Redirect("/admin/users", status_code=303)


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


@get("/teams")
async def admin_teams_get(
    page_session: NamedDependency[BrowserSession],
    provisioning: NamedDependency[ProvisioningService],
) -> Response:
    context = await _teams_context(page_session, provisioning)
    return render("SiteTeams", **context)


@dataclass
class NewTeamForm:
    name: str
    slug: str
    leader_email: str
    csrf_token: str


@post("/teams", status_code=200)
async def admin_teams_post(
    request: HTMXRequest,
    service: NamedDependency[PortalService],
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    data: Annotated[NewTeamForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    session = await require_verified_session(
        request,
        service,
        settings,
        data.csrf_token,
    )

    try:
        team = await provisioning.create_team(
            session.user.id,
            name=data.name,
            slug=data.slug,
            leader_email=data.leader_email,
        )
    except (PortalError, ValueError) as error:
        context = await _teams_context(
            session,
            provisioning,
            error=str(error),
        )
        return render("SiteTeams", **context)

    return Redirect(f"/teams/{team.id}/settings", status_code=303)


router = Router(
    path="/admin",
    route_handlers=[
        admin_home,
        admin_users_get,
        admin_users_post,
        admin_teams_get,
        admin_teams_post,
    ],
)
