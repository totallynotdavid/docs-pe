from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from litestar import Request, Response, get, post
from litestar.di import NamedDependency
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect
from litestar_htmx import HTMXRequest

from portal.application.provisioning import ProvisioningService
from portal.application.service import PortalService
from portal.domain.errors import PortalError
from portal.domain.models import BrowserSession, RequestTrace
from portal.settings import PortalSettings
from portal.web.deps import is_search_only, require_verified_session
from portal.web.render import render, render_hx


@dataclass
class FirstTeamForm:
    name: str
    csrf_token: str


@get("/")
async def dashboard(
    request: Request,
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
    provisioning: NamedDependency[ProvisioningService],
    settings: NamedDependency[PortalSettings],
) -> Response:
    if page_session.user.is_site_admin:
        status = await provisioning.installation_status(page_session.user.id)

        if status.can_create_first_team:
            return Redirect("/setup", status_code=303)

    teams = await service.teams(page_session.user.id)
    minimal = is_search_only(page_session.user, teams)

    def destination(team_id: UUID) -> str:
        return f"/teams/{team_id}/search" if minimal else f"/teams/{team_id}"

    # With a single team, or a remembered one from the last visit, there is no
    # real choice to present: skip straight past the picker. Search-only
    # sessions have always skipped it on a single team; a remembered team
    # removes the same hop for everyone else too.
    if len(teams) == 1:
        return Redirect(destination(teams[0].id), status_code=303)

    remembered = request.cookies.get(settings.last_team_cookie)
    match = next((team for team in teams if str(team.id) == remembered), None)

    if match is not None:
        return Redirect(destination(match.id), status_code=303)

    return render(
        "Dashboard",
        user=page_session.user,
        csrf_token=page_session.csrf_token,
        teams=teams,
        minimal=minimal,
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


@post("/setup", status_code=200)
async def first_team_post(
    request: HTMXRequest,
    service: NamedDependency[PortalService],
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    trace: NamedDependency[RequestTrace],
    data: Annotated[
        FirstTeamForm,
        Body(media_type=RequestEncodingType.URL_ENCODED),
    ],
) -> Response:
    session = await require_verified_session(
        request,
        settings,
        data.csrf_token,
    )

    try:
        team = await provisioning.create_first_team(
            session.user.id,
            name=data.name,
            trace=trace,
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
