from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import HTMLResponse

from portal.web.deps import PageSession, Service
from portal.web.render import render_hx


router = APIRouter()


@router.get("/equipos/{team_id}/buscar", response_class=HTMLResponse)
async def search(
    request: Request,
    session: PageSession,
    service: Service,
    team_id: UUID,
    q: str = "",
    page: int = 1,
) -> Response:
    results, has_more = await service.search(
        session.user.id, team_id, q, page=max(page, 1)
    )
    return render_hx(
        request,
        "search.html",
        "fragments/search_results.html",
        user=session.user,
        csrf_token=session.csrf_token,
        team=await service.team(session.user.id, team_id),
        query=q,
        results=results,
        page=max(page, 1),
        has_more=has_more,
    )
