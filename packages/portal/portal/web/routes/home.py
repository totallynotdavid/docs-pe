from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from litestar import Response, get, post
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
from portal.web.render import render, render_hx


@get("/")
async def dashboard(
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
    provisioning: NamedDependency[ProvisioningService],
) -> Response:
    if page_session.user.is_site_admin:
        status = await provisioning.installation_status(page_session.user.id)

        if status.can_create_first_team:
            return Redirect("/setup", status_code=303)

    return render(
        "Dashboard",
        user=page_session.user,
        csrf_token=page_session.csrf_token,
        teams=await service.teams(page_session.user.id),
    )


@get("/setup")
async def first_team_get(
    page_session: NamedDependency[BrowserSession],
    provisioning: NamedDependency[ProvisioningService],
) -> Response:
    status = await provisioning.installation_status(page_session.user.id)

    if not status.can_create_first_team:
        return Redirect("/", status_code=303)

    return render(
        "FirstTeam",
        user=page_session.user,
        csrf_token=page_session.csrf_token,
        error="",
        setup=True,
    )


@dataclass
class FirstTeamForm:
    name: str
    slug: str
    csrf_token: str


@post("/setup", status_code=200)
async def first_team_post(
    request: HTMXRequest,
    service: NamedDependency[PortalService],
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    data: Annotated[FirstTeamForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    session = await require_verified_session(
        request, service, settings, data.csrf_token
    )

    try:
        team = await provisioning.create_first_team(
            session.user.id,
            name=data.name,
            slug=data.slug,
        )
    except (PortalError, ValueError) as error:
        return render(
            "FirstTeam",
            user=session.user,
            csrf_token=session.csrf_token,
            error=str(error),
            setup=True,
        )

    return Redirect(f"/teams/{team.id}/settings/proxy", status_code=303)


@get("/notifications")
async def notifications(
    request: HTMXRequest,
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
) -> Response:
    return render_hx(
        request,
        "Notifications",
        "NotificationsFragment",
        user=page_session.user,
        csrf_token=page_session.csrf_token,
        notifications=await service.notifications(page_session.user.id),
    )


handlers = (dashboard, first_team_get, first_team_post, notifications)
