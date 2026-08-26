from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated
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
from portal.web.deps import is_search_only, require_verified_session
from portal.web.render import render, render_hx


if TYPE_CHECKING:
    from portal.domain.models import CredentialVersion


@get("")
async def team_page(
    request: HTMXRequest,
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
    provisioning: NamedDependency[ProvisioningService],
    team_id: FromPath[UUID],
    page: FromQuery[int] = 1,
) -> Response:
    current_page = max(page, 1)
    team = await service.team(page_session.user.id, team_id)
    can_manage = team.role is TeamRole.TEAM_LEADER or page_session.user.is_site_admin

    if not can_manage:
        return Redirect(f"/teams/{team_id}/search", status_code=303)

    jobs, total = await service.jobs(
        page_session.user.id,
        team_id,
        page=current_page,
    )
    teams = await service.teams(page_session.user.id)
    minimal = is_search_only(page_session.user, teams)
    readiness = await provisioning.team_readiness(page_session.user.id, team_id)

    return render_hx(
        request,
        "Team",
        "JobsFragment",
        user=page_session.user,
        csrf_token=page_session.csrf_token,
        team=team,
        teams=teams,
        jobs=jobs,
        total=total,
        page=current_page,
        minimal=minimal,
        readiness=readiness,
    )


@get("/settings")
async def team_settings_overview(
    page_session: NamedDependency[BrowserSession],
    team_id: FromPath[UUID],
) -> Response:
    del page_session
    return Redirect(f"/teams/{team_id}/settings/proxy", status_code=303)


@get("/settings/search-activity")
async def team_search_activity_get(
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
    team_id: FromPath[UUID],
) -> Response:
    return render(
        "TeamSearchActivity",
        user=page_session.user,
        csrf_token=page_session.csrf_token,
        team=await service.team(page_session.user.id, team_id),
        teams=await service.teams(page_session.user.id),
        entries=await service.recent_searches(page_session.user.id, team_id),
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
    teams = await service.teams(session.user.id)
    members = await provisioning.members(session.user.id, team_id)
    invites = await provisioning.pending_invites(session.user.id, team_id)

    return {
        "user": session.user,
        "csrf_token": session.csrf_token,
        "team": team,
        "teams": teams,
        "members": members,
        "invites": invites,
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


@dataclass
class InviteActionForm:
    csrf_token: str


@post("/settings/invites/{invite_id:uuid}/resend", status_code=200)
async def team_invite_resend(
    request: HTMXRequest,
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    trace: NamedDependency[RequestTrace],
    team_id: FromPath[UUID],
    invite_id: FromPath[UUID],
    data: Annotated[InviteActionForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    session = await require_verified_session(request, settings, data.csrf_token)

    await provisioning.resend_invite(
        session.user.id,
        team_id=team_id,
        invite_id=invite_id,
        trace=trace,
    )

    return Redirect(f"/teams/{team_id}/settings/members", status_code=303)


@post("/settings/invites/{invite_id:uuid}/cancel", status_code=200)
async def team_invite_cancel(
    request: HTMXRequest,
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    team_id: FromPath[UUID],
    invite_id: FromPath[UUID],
    data: Annotated[InviteActionForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    session = await require_verified_session(request, settings, data.csrf_token)

    await provisioning.cancel_invite(
        session.user.id,
        team_id=team_id,
        invite_id=invite_id,
    )

    return Redirect(f"/teams/{team_id}/settings/members", status_code=303)


def _latest_per_connection(
    credentials: tuple[CredentialVersion, ...],
) -> list[CredentialVersion]:
    """One row per connection, its newest version. credentials_for_team is
    already ordered label, version DESC, so the first row seen per
    credential_id is that connection's current one."""
    latest: dict[UUID, CredentialVersion] = {}

    for credential in credentials:
        latest.setdefault(credential.credential_id, credential)

    return list(latest.values())


async def _proxy_context(
    session: BrowserSession,
    service: PortalService,
    provisioning: ProvisioningService,
    team_id: UUID,
    *,
    provider: str,
    show_form: bool,
    edit_credential_id: UUID | None = None,
    error: str = "",
) -> dict[str, object]:
    team = await service.team(session.user.id, team_id)
    teams = await service.teams(session.user.id)
    credentials = await service.credentials(session.user.id, team_id)
    readiness = await provisioning.team_readiness(session.user.id, team_id)
    connections = _latest_per_connection(credentials)

    editing = next(
        (
            connection
            for connection in connections
            if connection.credential_id == edit_credential_id
        ),
        None,
    )
    # Editing keeps the connection's own provider: switching providers isn't
    # "editing", it's replacing the connection, which "Añadir conexión" already does.
    active_provider = editing.provider if editing else provider
    fields = ProvisioningService.provider_fields(active_provider)

    return {
        "user": session.user,
        "csrf_token": session.csrf_token,
        "team": team,
        "teams": teams,
        "connections": connections,
        "readiness": readiness,
        "editing": editing,
        "provider": active_provider,
        "providers": provider_names(),
        "basic_fields": tuple(field for field in fields if not field.advanced),
        "advanced_fields": tuple(field for field in fields if field.advanced),
        # Progressive disclosure: a team with connections already sees the
        # list first: the form is a deliberate "+ Add" action, not something
        # to fill in on every visit. A team with none goes straight to it.
        "show_form": show_form or not connections,
        "error": error,
    }


@get("/settings/proxy")
async def proxy_settings_get(
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
    provisioning: NamedDependency[ProvisioningService],
    team_id: FromPath[UUID],
    provider: FromQuery[str] = "geonode",
    add: FromQuery[bool] = False,
    edit: FromQuery[UUID | None] = None,
) -> Response:
    context = await _proxy_context(
        page_session,
        service,
        provisioning,
        team_id,
        provider=provider,
        show_form=add or edit is not None,
        edit_credential_id=edit,
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
    raw_edit_id = str(form.get("edit_credential_id", ""))
    edit_credential_id = UUID(raw_edit_id) if raw_edit_id else None
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
            show_form=True,
            edit_credential_id=edit_credential_id,
            error=message_for(error),
        )

        return render("ProxySettings", **context)

    return Redirect(f"/teams/{team_id}/settings/proxy", status_code=303)


@dataclass
class RetireCredentialForm:
    csrf_token: str


@post("/settings/proxy/{credential_id:uuid}/retire", status_code=200)
async def proxy_credential_retire(
    request: HTMXRequest,
    service: NamedDependency[PortalService],
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    team_id: FromPath[UUID],
    credential_id: FromPath[UUID],
    data: Annotated[
        RetireCredentialForm,
        Body(media_type=RequestEncodingType.URL_ENCODED),
    ],
) -> Response:
    session = await require_verified_session(
        request,
        settings,
        data.csrf_token,
    )

    try:
        await service.retire_credential(session.user.id, team_id, credential_id)
    except PortalError as error:
        context = await _proxy_context(
            session,
            service,
            provisioning,
            team_id,
            provider="geonode",
            show_form=False,
            error=message_for(error),
        )

        return render("ProxySettings", **context)

    return Redirect(f"/teams/{team_id}/settings/proxy", status_code=303)


@dataclass
class RenameCredentialForm:
    label: str
    csrf_token: str


@post("/settings/proxy/{credential_id:uuid}/rename", status_code=200)
async def proxy_credential_rename(
    request: HTMXRequest,
    service: NamedDependency[PortalService],
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    team_id: FromPath[UUID],
    credential_id: FromPath[UUID],
    data: Annotated[
        RenameCredentialForm,
        Body(media_type=RequestEncodingType.URL_ENCODED),
    ],
) -> Response:
    session = await require_verified_session(
        request,
        settings,
        data.csrf_token,
    )

    try:
        await service.rename_credential(
            session.user.id,
            team_id,
            credential_id,
            data.label,
        )
    except PortalError as error:
        context = await _proxy_context(
            session,
            service,
            provisioning,
            team_id,
            provider="geonode",
            show_form=False,
            error=message_for(error),
        )

        return render("ProxySettings", **context)

    return Redirect(f"/teams/{team_id}/settings/proxy", status_code=303)


router = Router(
    path="/teams/{team_id:uuid}",
    route_handlers=[
        team_page,
        team_settings_overview,
        team_search_activity_get,
        team_members_get,
        team_members_post,
        team_members_remove,
        team_invite_resend,
        team_invite_cancel,
        proxy_settings_get,
        proxy_settings_post,
        proxy_credential_rename,
        proxy_credential_retire,
    ],
)
