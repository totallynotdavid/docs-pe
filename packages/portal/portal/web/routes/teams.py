from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from litestar import Response, Router, get, post
from litestar.di import NamedDependency
from litestar.enums import RequestEncodingType
from litestar.params import Body, FromPath, FromQuery
from litestar.response import Redirect
from litestar_htmx import HTMXRequest

from portal.application.provisioning import ProvisioningService
from portal.application.service import PortalService
from portal.domain.errors import PortalError
from portal.domain.models import BrowserSession, RequestTrace, TeamRole
from portal.messages import message_for, provider_names
from portal.settings import PortalSettings
from portal.web.deps import require_verified_session
from portal.web.render import render, render_hx


@get("")
async def team_page(
    request: HTMXRequest,
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
    team_id: FromPath[UUID],
    page: FromQuery[int] = 1,
) -> Response:
    current_page = max(page, 1)
    team = await service.team(page_session.user.id, team_id)
    jobs, total = await service.jobs(
        page_session.user.id,
        team_id,
        page=current_page,
    )

    return render_hx(
        request,
        "Team",
        "JobsFragment",
        user=page_session.user,
        csrf_token=page_session.csrf_token,
        team=team,
        jobs=jobs,
        total=total,
        page=current_page,
    )


@get("/settings")
async def team_settings_overview(
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
    provisioning: NamedDependency[ProvisioningService],
    team_id: FromPath[UUID],
) -> Response:
    team = await service.team(page_session.user.id, team_id)
    readiness = await provisioning.team_readiness(page_session.user.id, team_id)

    return render(
        "TeamSettings",
        user=page_session.user,
        csrf_token=page_session.csrf_token,
        team=team,
        readiness=readiness,
    )


async def _members_context(
    session: BrowserSession,
    service: PortalService,
    provisioning: ProvisioningService,
    team_id: UUID,
    *,
    error: str = "",
) -> dict[str, object]:
    team = await service.team(session.user.id, team_id)
    members = await provisioning.members(session.user.id, team_id)
    candidates = await provisioning.member_candidates(session.user.id, team_id)

    return {
        "user": session.user,
        "csrf_token": session.csrf_token,
        "team": team,
        "members": members,
        "candidates": candidates,
        "error": error,
    }


@get("/settings/members")
async def team_members_get(
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
    provisioning: NamedDependency[ProvisioningService],
    team_id: FromPath[UUID],
) -> Response:
    context = await _members_context(
        page_session,
        service,
        provisioning,
        team_id,
    )

    return render("TeamMembers", **context)


@dataclass
class MemberForm:
    email: str
    role: TeamRole
    csrf_token: str


@post("/settings/members", status_code=200)
async def team_members_post(
    request: HTMXRequest,
    service: NamedDependency[PortalService],
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    trace: NamedDependency[RequestTrace],
    team_id: FromPath[UUID],
    data: Annotated[MemberForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    session = await require_verified_session(
        request,
        settings,
        data.csrf_token,
    )

    try:
        await provisioning.invite_or_add_member(
            session.user.id,
            team_id=team_id,
            email=data.email,
            role=data.role,
            trace=trace,
        )
    except PortalError as error:
        context = await _members_context(
            session,
            service,
            provisioning,
            team_id,
            error=message_for(error),
        )

        return render("TeamMembers", **context)

    return Redirect(f"/teams/{team_id}/settings/members", status_code=303)


@dataclass
class RemoveMemberForm:
    email: str
    csrf_token: str


@post("/settings/members/remove", status_code=200)
async def team_members_remove(
    request: HTMXRequest,
    service: NamedDependency[PortalService],
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    trace: NamedDependency[RequestTrace],
    team_id: FromPath[UUID],
    data: Annotated[RemoveMemberForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    session = await require_verified_session(
        request,
        settings,
        data.csrf_token,
    )

    await provisioning.remove_member(
        session.user.id,
        team_id=team_id,
        email=data.email,
        trace=trace,
    )

    return Redirect(f"/teams/{team_id}/settings/members", status_code=303)


async def _proxy_context(
    session: BrowserSession,
    service: PortalService,
    provisioning: ProvisioningService,
    team_id: UUID,
    *,
    provider: str,
    error: str = "",
) -> dict[str, object]:
    team = await service.team(session.user.id, team_id)
    credentials = await service.credentials(session.user.id, team_id)
    readiness = await provisioning.team_readiness(session.user.id, team_id)

    return {
        "user": session.user,
        "csrf_token": session.csrf_token,
        "team": team,
        "credentials": credentials,
        "readiness": readiness,
        "provider": provider,
        "providers": provider_names(),
        "fields": ProvisioningService.provider_fields(provider),
        "error": error,
    }


@get("/settings/proxy")
async def proxy_settings_get(
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
    provisioning: NamedDependency[ProvisioningService],
    team_id: FromPath[UUID],
    provider: FromQuery[str] = "geonode",
) -> Response:
    context = await _proxy_context(
        page_session,
        service,
        provisioning,
        team_id,
        provider=provider,
    )

    return render("ProxySettings", **context)


@post("/settings/proxy", status_code=200)
async def proxy_settings_post(
    request: HTMXRequest,
    service: NamedDependency[PortalService],
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    trace: NamedDependency[RequestTrace],
    team_id: FromPath[UUID],
) -> Response:
    # Proxy fields depend on the selected provider, so this route reads the raw form.
    form = await request.form()

    session = await require_verified_session(
        request,
        settings,
        str(form.get("csrf_token", "")),
    )

    provider = str(form.get("provider", ""))
    label = str(form.get("label", ""))
    values = {
        field.name: str(form.get(field.name, ""))
        for field in ProvisioningService.provider_fields(provider)
    }

    try:
        await provisioning.configure_proxy(
            session.user.id,
            team_id=team_id,
            label=label,
            provider=provider,
            values=values,
            trace=trace,
        )
    except PortalError as error:
        context = await _proxy_context(
            session,
            service,
            provisioning,
            team_id,
            provider=provider,
            error=message_for(error),
        )

        return render("ProxySettings", **context)

    return Redirect(f"/teams/{team_id}/settings/proxy", status_code=303)


router = Router(
    path="/teams/{team_id:uuid}",
    route_handlers=[
        team_page,
        team_settings_overview,
        team_members_get,
        team_members_post,
        team_members_remove,
        proxy_settings_get,
        proxy_settings_post,
    ],
)
