from __future__ import annotations

from uuid import UUID

from litestar import Response, Router, get
from litestar.di import NamedDependency
from litestar.params import FromPath, FromQuery
from litestar_htmx import HTMXRequest

from portal.application.service import PortalService
from portal.domain.models import BrowserSession
from portal.web.deps import is_search_only
from portal.web.render import render, render_hx


@get("/search")
async def search(
    request: HTMXRequest,
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
    team_id: FromPath[UUID],
    q: FromQuery[str] = "",
    page: FromQuery[int] = 1,
) -> Response:
    current_page = max(page, 1)

    results, has_more = await service.search(
        page_session.user.id,
        team_id,
        q,
        page=current_page,
    )
    team = await service.team(page_session.user.id, team_id)
    minimal = is_search_only(
        page_session.user,
        await service.teams(page_session.user.id),
    )

    return render_hx(
        request,
        "Search",
        "SearchResultsFragment",
        user=page_session.user,
        csrf_token=page_session.csrf_token,
        team=team,
        query=q,
        results=results,
        page=current_page,
        has_more=has_more,
        minimal=minimal,
    )


@get("/entries/{entry_id:uuid}")
async def entry_detail(
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
    team_id: FromPath[UUID],
    entry_id: FromPath[UUID],
) -> Response:
    entry = await service.entry(page_session.user.id, team_id, entry_id)
    team = await service.team(page_session.user.id, team_id)
    minimal = is_search_only(
        page_session.user,
        await service.teams(page_session.user.id),
    )

    return render(
        "EntryDetail",
        user=page_session.user,
        csrf_token=page_session.csrf_token,
        team=team,
        entry=entry,
        minimal=minimal,
        back_href=f"/teams/{team_id}/search?q={entry.document}",
    )


router = Router(
    path="/teams/{team_id:uuid}",
    route_handlers=[search, entry_detail],
)
