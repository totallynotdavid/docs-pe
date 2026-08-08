from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from litestar import Response, Router, get, post
from litestar.di import NamedDependency
from litestar.enums import RequestEncodingType
from litestar.exceptions import HTTPException
from litestar.params import Body, FromPath
from litestar.response import Redirect
from litestar_htmx import HTMXRequest

from portal.application.provisioning import ProvisioningService
from portal.application.service import PortalService
from portal.domain.errors import PortalError, StepUpRequired
from portal.domain.models import BrowserSession, RequestTrace
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
    trace: NamedDependency[RequestTrace],
    data: Annotated[NewUserForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    session = await require_verified_session(
        request,
        settings,
        data.csrf_token,
    )

    try:
        await provisioning.create_user(
            session.user.id,
            email=data.email,
            password=data.password,
            mfa_verified_at=session.mfa_verified_at,
            trace=trace,
        )
    except StepUpRequired:
        raise
    except (PortalError, ValueError) as error:
        context = await _users_context(
            session,
            provisioning,
            error=str(error),
        )
        return render("SiteUsers", **context)

    return Redirect("/admin/users", status_code=303)


async def _user_detail_context(
    session: BrowserSession,
    provisioning: ProvisioningService,
    user_id: UUID,
    *,
    error: str = "",
) -> dict[str, object]:
    return {
        "user": session.user,
        "csrf_token": session.csrf_token,
        "person": await provisioning.user_detail(session.user.id, user_id),
        "teams": await provisioning.teams_for_user(session.user.id, user_id),
        "is_sole_active_admin": await provisioning.is_sole_active_admin(
            session.user.id,
            user_id,
        ),
        "error": error,
    }


@get("/users/{user_id:uuid}")
async def admin_user_detail_get(
    page_session: NamedDependency[BrowserSession],
    provisioning: NamedDependency[ProvisioningService],
    user_id: FromPath[UUID],
) -> Response:
    context = await _user_detail_context(page_session, provisioning, user_id)
    return render("UserDetail", **context)


@dataclass
class UserActionForm:
    action: str
    csrf_token: str
    password: str = ""


@post("/users/{user_id:uuid}", status_code=200)
async def admin_user_action_post(
    request: HTMXRequest,
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    trace: NamedDependency[RequestTrace],
    user_id: FromPath[UUID],
    data: Annotated[UserActionForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    session = await require_verified_session(
        request,
        settings,
        data.csrf_token,
    )

    try:
        if data.action == "deactivate":
            await provisioning.deactivate_user(
                session.user.id,
                user_id=user_id,
                mfa_verified_at=session.mfa_verified_at,
                trace=trace,
            )
        elif data.action == "reactivate":
            await provisioning.reactivate_user(
                session.user.id,
                user_id=user_id,
                mfa_verified_at=session.mfa_verified_at,
                trace=trace,
            )
        elif data.action == "delete":
            await provisioning.delete_user(
                session.user.id,
                user_id=user_id,
                mfa_verified_at=session.mfa_verified_at,
                trace=trace,
            )
            return Redirect("/admin/users", status_code=303)
        elif data.action == "promote":
            # Never generates or shows a second factor here: a target with
            # none yet is only marked pending_site_admin and completes their
            # own enrollment at /security/setup on next login.
            await provisioning.promote_to_site_admin(
                session.user.id,
                user_id=user_id,
                mfa_verified_at=session.mfa_verified_at,
                trace=trace,
            )
        elif data.action == "demote":
            await provisioning.demote_site_admin(
                session.user.id,
                user_id=user_id,
                mfa_verified_at=session.mfa_verified_at,
                trace=trace,
            )
        elif data.action == "reset_password":
            await provisioning.reset_password(
                session.user.id,
                user_id=user_id,
                password=data.password,
                mfa_verified_at=session.mfa_verified_at,
                trace=trace,
            )
        else:
            raise HTTPException(status_code=400, detail="unknown action")
    except StepUpRequired:
        raise
    except (PortalError, ValueError) as error:
        context = await _user_detail_context(
            session,
            provisioning,
            user_id,
            error=str(error),
        )
        return render("UserDetail", **context)

    return Redirect(f"/admin/users/{user_id}", status_code=303)


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
    trace: NamedDependency[RequestTrace],
    data: Annotated[NewTeamForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    session = await require_verified_session(
        request,
        settings,
        data.csrf_token,
    )

    try:
        team = await provisioning.create_team(
            session.user.id,
            name=data.name,
            slug=data.slug,
            leader_email=data.leader_email,
            mfa_verified_at=session.mfa_verified_at,
            trace=trace,
        )
    except StepUpRequired:
        raise
    except (PortalError, ValueError) as error:
        context = await _teams_context(
            session,
            provisioning,
            error=str(error),
        )
        return render("SiteTeams", **context)

    return Redirect(f"/teams/{team.id}/settings", status_code=303)


@get("/search-activity")
async def admin_search_activity_get(
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
) -> Response:
    return render(
        "SiteSearchActivity",
        user=page_session.user,
        csrf_token=page_session.csrf_token,
        activity=await service.team_search_activity(page_session.user.id),
    )


@get("/system")
async def admin_system_get(
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
) -> Response:
    return render(
        "SiteSystem",
        user=page_session.user,
        csrf_token=page_session.csrf_token,
        health=await service.system_health(page_session.user.id),
    )


router = Router(
    path="/admin",
    route_handlers=[
        admin_home,
        admin_users_get,
        admin_users_post,
        admin_user_detail_get,
        admin_user_action_post,
        admin_teams_get,
        admin_teams_post,
        admin_search_activity_get,
        admin_system_get,
    ],
)
