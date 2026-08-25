from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated
from urllib.parse import quote

from litestar import Request, Response, get, post
from litestar.di import NamedDependency
from litestar.enums import RequestEncodingType
from litestar.params import Body, FromQuery
from litestar.response import Redirect

from portal.application.login import LoginService
from portal.application.sessions import BrowserSessions
from portal.domain.models import BrowserSession, RequestTrace
from portal.settings import PortalSettings
from portal.web.deps import require_verified_session
from portal.web.render import render


@dataclass
class StepUpForm:
    code: str
    csrf_token: str
    next_path: str = "/"


def _safe_next(candidate: str) -> str:
    """A same-origin relative path only. Letting next_path name a full URL
    would turn this into an open redirect off a trusted, authenticated
    origin."""
    if (
        candidate.startswith("/")
        and not candidate.startswith("//")
        and "://" not in candidate
    ):
        return candidate

    return "/"


@get("/step-up")
async def step_up_get(
    page_session: NamedDependency[BrowserSession],
    next_path: FromQuery[str] = "/",
    error: FromQuery[str] = "",
) -> Response:
    return render(
        "StepUp",
        # Use the centered signed-out shell while preserving the session's
        # CSRF token for the form.
        user=None,
        csrf_token=page_session.csrf_token,
        next_path=_safe_next(next_path),
        error=error,
    )


@post("/step-up", status_code=200)
async def step_up_post(
    request: Request,
    login: NamedDependency[LoginService],
    sessions: NamedDependency[BrowserSessions],
    settings: NamedDependency[PortalSettings],
    trace: NamedDependency[RequestTrace],
    data: Annotated[StepUpForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    session = await require_verified_session(request, settings, data.csrf_token)
    target = _safe_next(data.next_path)

    if not await login.verify_second_factor(session.user, data.code, trace):
        return Redirect(
            f"/step-up?next_path={quote(target, safe='/')}&error=1", status_code=303
        )

    await sessions.mark_step_up_verified(request.cookies.get(settings.session_cookie))

    return Redirect(target, status_code=303)


handlers = (step_up_get, step_up_post)
