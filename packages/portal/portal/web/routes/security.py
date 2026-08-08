from __future__ import annotations

import json

from dataclasses import dataclass
from typing import Annotated, Any
from uuid import UUID

from litestar import Request, Response, Router, get, post
from litestar.di import NamedDependency
from litestar.enums import RequestEncodingType
from litestar.params import Body
from litestar.response import Redirect

from portal.application.provisioning import ProvisioningService
from portal.application.sessions import BrowserSessions
from portal.domain.errors import PortalError
from portal.domain.models import BrowserSession
from portal.messages import message_for
from portal.settings import PortalSettings
from portal.web.deps import require_verified_session
from portal.web.render import render


async def _security_context(
    session: BrowserSession,
    provisioning: ProvisioningService,
    *,
    error: str = "",
    recovery_codes: tuple[str, ...] | None = None,
    totp_setup_token: str = "",
    totp_enrollment_uri: str = "",
) -> dict[str, object]:
    return {
        "user": session.user,
        "csrf_token": session.csrf_token,
        "setup": session.user.pending_site_admin,
        "passkeys": await provisioning.passkeys(session.user.id),
        "error": error,
        "recovery_codes": recovery_codes,
        "totp_setup_token": totp_setup_token,
        "totp_enrollment_uri": totp_enrollment_uri,
    }


@get("")
async def security_get(
    page_session: NamedDependency[BrowserSession],
    provisioning: NamedDependency[ProvisioningService],
) -> Response:
    context = await _security_context(page_session, provisioning)
    return render("Security", **context)


@get("/totp/setup")
async def security_totp_setup_get(
    page_session: NamedDependency[BrowserSession],
    provisioning: NamedDependency[ProvisioningService],
) -> Response:
    # A GET with a side effect: it only ever writes a fresh, short-lived
    # setup token nobody but this browser will see the response for, the
    # same reasoning that lets /login/mfa read cookie state without a body.
    setup = await provisioning.begin_totp_setup(page_session.user.id)
    context = await _security_context(
        page_session,
        provisioning,
        totp_setup_token=setup.setup_token,
        totp_enrollment_uri=setup.enrollment_uri,
    )
    return render("Security", **context)


@dataclass
class TotpConfirmForm:
    setup_token: str
    code: str
    csrf_token: str


@post("/totp/confirm", status_code=200)
async def security_totp_confirm_post(
    request: Request,
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    sessions: NamedDependency[BrowserSessions],
    data: Annotated[TotpConfirmForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    session = await require_verified_session(request, settings, data.csrf_token)

    try:
        recovery_codes = await provisioning.confirm_totp_setup(
            session.user.id,
            setup_token=data.setup_token,
            code=data.code,
        )
    except PortalError as error:
        context = await _security_context(
            session,
            provisioning,
            error=message_for(error),
        )
        return render("Security", **context)

    # A live TOTP confirmation is fresh second-factor proof: no reason to
    # send someone who just typed a code straight back through /step-up.
    await sessions.mark_step_up_verified(request.cookies.get(settings.session_cookie))

    if recovery_codes is None:
        return Redirect("/security", status_code=303)

    context = await _security_context(session, provisioning, recovery_codes=recovery_codes)
    return render("Security", **context)


@dataclass
class CsrfOnlyForm:
    csrf_token: str


@post("/totp/disable", status_code=200)
async def security_totp_disable_post(
    request: Request,
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    data: Annotated[CsrfOnlyForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    session = await require_verified_session(request, settings, data.csrf_token)

    try:
        await provisioning.disable_totp(session.user.id)
    except PortalError as error:
        context = await _security_context(
            session,
            provisioning,
            error=message_for(error),
        )
        return render("Security", **context)

    return Redirect("/security", status_code=303)


@dataclass
class PasskeyOptionsForm:
    csrf_token: str


@post("/passkey/options", status_code=200)
async def security_passkey_options_post(
    request: Request,
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    data: PasskeyOptionsForm,
) -> Response:
    session = await require_verified_session(request, settings, data.csrf_token)
    setup = await provisioning.begin_passkey_registration(session.user.id)

    return Response(
        content={
            "setupToken": setup.setup_token,
            "options": json.loads(setup.options_json),
        },
        media_type="application/json",
    )


@dataclass
class PasskeyRegisterForm:
    csrf_token: str
    setup_token: str
    response: dict[str, Any]
    label: str = ""


@post("/passkey/register", status_code=200)
async def security_passkey_register_post(
    request: Request,
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    sessions: NamedDependency[BrowserSessions],
    data: PasskeyRegisterForm,
) -> Response:
    session = await require_verified_session(request, settings, data.csrf_token)

    try:
        recovery_codes = await provisioning.confirm_passkey_registration(
            session.user.id,
            setup_token=data.setup_token,
            response_json=json.dumps(data.response),
            label=data.label,
        )
    except PortalError as error:
        return Response(
            content={"error": message_for(error)},
            status_code=400,
            media_type="application/json",
        )

    # The registration ceremony itself is fresh second-factor proof.
    await sessions.mark_step_up_verified(request.cookies.get(settings.session_cookie))

    return Response(
        content={
            "recoveryCodes": list(recovery_codes) if recovery_codes is not None else None,
        },
        media_type="application/json",
    )


@dataclass
class PasskeyRemoveForm:
    credential_id: UUID
    csrf_token: str


@post("/passkey/remove", status_code=200)
async def security_passkey_remove_post(
    request: Request,
    settings: NamedDependency[PortalSettings],
    provisioning: NamedDependency[ProvisioningService],
    data: Annotated[PasskeyRemoveForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    session = await require_verified_session(request, settings, data.csrf_token)

    try:
        await provisioning.remove_passkey(session.user.id, credential_id=data.credential_id)
    except PortalError as error:
        context = await _security_context(
            session,
            provisioning,
            error=message_for(error),
        )
        return render("Security", **context)

    return Redirect("/security", status_code=303)


router = Router(
    path="/security",
    route_handlers=[
        security_get,
        security_totp_setup_get,
        security_totp_confirm_post,
        security_totp_disable_post,
        security_passkey_options_post,
        security_passkey_register_post,
        security_passkey_remove_post,
    ],
)
