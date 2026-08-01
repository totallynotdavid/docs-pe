from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from portal.application.provisioning import ProvisioningService
from portal.application.service import PortalService
from portal.domain.errors import PortalError
from portal.domain.models import BrowserSession, ProxyProvider, TeamRole
from portal.web.deps import PageSession, Provisioning, Service, VerifiedSession
from portal.web.render import render, render_hx


router = APIRouter(prefix="/equipos/{team_id}")


@router.get("", response_class=HTMLResponse)
async def team_page(
    request: Request,
    session: PageSession,
    service: Service,
    team_id: UUID,
    page: int = 1,
) -> Response:
    team = await service.team(session.user.id, team_id)
    jobs, total = await service.jobs(session.user.id, team_id, page=max(page, 1))
    return render_hx(
        request,
        "team.html",
        "fragments/jobs.html",
        user=session.user,
        csrf_token=session.csrf_token,
        team=team,
        jobs=jobs,
        total=total,
        page=max(page, 1),
    )


@router.get("/ajustes", response_class=HTMLResponse)
async def team_settings_overview(
    session: PageSession, service: Service, provisioning: Provisioning, team_id: UUID
) -> Response:
    return render(
        "team_settings.html",
        user=session.user,
        csrf_token=session.csrf_token,
        team=await service.team(session.user.id, team_id),
        readiness=await provisioning.team_readiness(session.user.id, team_id),
    )


async def _members_context(
    session: BrowserSession,
    service: PortalService,
    provisioning: ProvisioningService,
    team_id: UUID,
    *,
    error: str,
) -> dict[str, object]:
    return {
        "user": session.user,
        "csrf_token": session.csrf_token,
        "team": await service.team(session.user.id, team_id),
        "members": await provisioning.members(session.user.id, team_id),
        "candidates": await provisioning.member_candidates(session.user.id, team_id),
        "error": error,
    }


@router.get("/ajustes/miembros", response_class=HTMLResponse)
async def team_members_get(
    session: PageSession, service: Service, provisioning: Provisioning, team_id: UUID
) -> Response:
    context = await _members_context(session, service, provisioning, team_id, error="")
    return render("team_members.html", **context)


@router.post("/ajustes/miembros", response_class=HTMLResponse)
async def team_members_post(
    session: VerifiedSession,
    service: Service,
    provisioning: Provisioning,
    team_id: UUID,
    email: str = Form(),
    role: TeamRole = Form(),
) -> Response:
    try:
        await provisioning.invite_or_add_member(
            session.user.id, team_id=team_id, email=email, role=role
        )
    except (PortalError, ValueError) as error:
        context = await _members_context(
            session, service, provisioning, team_id, error=str(error)
        )
        return render("team_members.html", **context)
    return RedirectResponse(f"/equipos/{team_id}/ajustes/miembros", status_code=303)


@router.post("/ajustes/miembros/quitar")
async def team_members_remove(
    session: VerifiedSession,
    provisioning: Provisioning,
    team_id: UUID,
    email: str = Form(),
) -> Response:
    try:
        await provisioning.remove_member(session.user.id, team_id=team_id, email=email)
    except ValueError as error:
        # Removing the last leader is refused by the repository, not the planner.
        raise HTTPException(status_code=403, detail=str(error)) from error
    return RedirectResponse(f"/equipos/{team_id}/ajustes/miembros", status_code=303)


async def _proxy_context(
    session: BrowserSession,
    service: PortalService,
    provisioning: ProvisioningService,
    team_id: UUID,
    *,
    provider: ProxyProvider,
    error: str,
) -> dict[str, object]:
    return {
        "user": session.user,
        "csrf_token": session.csrf_token,
        "team": await service.team(session.user.id, team_id),
        "credentials": await service.credentials(session.user.id, team_id),
        "readiness": await provisioning.team_readiness(session.user.id, team_id),
        "provider": provider,
        "fields": ProvisioningService.provider_fields(provider),
        "error": error,
    }


@router.get("/ajustes/proxy", response_class=HTMLResponse)
async def proxy_settings_get(
    session: PageSession,
    service: Service,
    provisioning: Provisioning,
    team_id: UUID,
    proveedor: ProxyProvider = ProxyProvider.GEONODE,
) -> Response:
    context = await _proxy_context(
        session, service, provisioning, team_id, provider=proveedor, error=""
    )
    return render("proxy_settings.html", **context)


@router.post("/ajustes/proxy", response_class=HTMLResponse)
async def proxy_settings_post(
    session: VerifiedSession,
    service: Service,
    provisioning: Provisioning,
    team_id: UUID,
    label: str = Form(),
    provider: ProxyProvider = Form(),
    username: str = Form(""),
    password: str = Form(""),
    gateway: str = Form(""),
    proxy_type: str = Form(""),
    country: str = Form(""),
    state: str = Form(""),
    city: str = Form(""),
    asn: str = Form(""),
    lifetime_minutes: str = Form(""),
    session_minutes: str = Form(""),
) -> Response:
    try:
        await provisioning.configure_proxy(
            session.user.id,
            team_id=team_id,
            label=label,
            provider=provider,
            values={
                "username": username,
                "password": password,
                "gateway": gateway,
                "proxy_type": proxy_type,
                "country": country,
                "state": state,
                "city": city,
                "asn": asn,
                "lifetime_minutes": lifetime_minutes,
                "session_minutes": session_minutes,
            },
        )
    except (PortalError, ValueError) as error:
        context = await _proxy_context(
            session, service, provisioning, team_id, provider=provider, error=str(error)
        )
        return render("proxy_settings.html", **context)
    return RedirectResponse(f"/equipos/{team_id}/ajustes/proxy", status_code=303)
