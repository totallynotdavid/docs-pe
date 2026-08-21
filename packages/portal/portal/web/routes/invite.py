from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from litestar import Response, get, post
from litestar.di import NamedDependency
from litestar.enums import RequestEncodingType
from litestar.params import Body, FromPath
from litestar.response import Redirect

from portal.application.provisioning import ProvisioningService
from portal.application.sessions import BrowserSessions
from portal.domain.errors import NotFound, PortalError
from portal.messages import message_for
from portal.settings import PortalSettings
from portal.web.deps import require_same_origin
from portal.web.render import render


@get("/invite/{token:str}")
async def invite_get(
    provisioning: NamedDependency[ProvisioningService],
    token: FromPath[str],
) -> Response:
    invite = await provisioning.invite_preview(token=token)

    return render(
        "AcceptInvite",
        token=token,
        email=invite.email,
        error="",
    )


@dataclass
class AcceptInviteForm:
    password: str


@post("/invite/{token:str}", guards=[require_same_origin], status_code=200)
async def invite_post(
    provisioning: NamedDependency[ProvisioningService],
    sessions: NamedDependency[BrowserSessions],
    settings: NamedDependency[PortalSettings],
    token: FromPath[str],
    data: Annotated[AcceptInviteForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    try:
        user = await provisioning.redeem_invite(token=token, password=data.password)
    except NotFound:
        raise
    except PortalError as error:
        invite = await provisioning.invite_preview(token=token)
        return render(
            "AcceptInvite",
            token=token,
            email=invite.email,
            error=message_for(error),
        )

    cookie_token = await sessions.mint(user.id)

    response: Response = Redirect("/security", status_code=303)
    response.set_cookie(
        settings.session_cookie,
        cookie_token,
        path="/",
        secure=settings.serves_https,
        httponly=True,
        samesite="strict",
    )

    return response


handlers = (invite_get, invite_post)
