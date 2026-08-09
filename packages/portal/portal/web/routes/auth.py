from __future__ import annotations

import json

from dataclasses import dataclass
from typing import Annotated

from litestar import Request, Response, get, post
from litestar.di import NamedDependency
from litestar.enums import RequestEncodingType
from litestar.params import Body, FromQuery
from litestar.response import Redirect

from portal.application.login import (
    LoginAttempt,
    LoginService,
    MfaAttempt,
    MfaChallengeIssued,
    PasskeyLoginAttempt,
    SessionIssued,
)
from portal.domain.models import BrowserSession, RequestTrace
from portal.settings import PortalSettings
from portal.web.deps import require_same_origin, require_verified_session
from portal.web.render import render


@dataclass
class LoginForm:
    email: str
    password: str
    csrf_token: str
    turnstile_token: str = ""


@dataclass
class MfaForm:
    code: str


@dataclass
class PasskeyLoginForm:
    login_token: str
    response: str


@dataclass
class LogoutForm:
    csrf_token: str


@get("/login")
async def login_get(
    login: NamedDependency[LoginService],
    settings: NamedDependency[PortalSettings],
    optional_session: NamedDependency[BrowserSession | None],
    error: FromQuery[str] = "",
) -> Response:
    if optional_session is not None:
        return Redirect("/", status_code=303)

    return render(
        "Login",
        user=None,
        csrf_token=await login.issue_login_csrf(),
        turnstile_site_key=settings.turnstile_site_key,
        error=error,
    )


@post("/login", guards=[require_same_origin], status_code=200)
async def login_post(
    request: Request,
    login: NamedDependency[LoginService],
    settings: NamedDependency[PortalSettings],
    trace: NamedDependency[RequestTrace],
    data: Annotated[LoginForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    outcome = await login.attempt(
        LoginAttempt(
            email=data.email,
            password=data.password,
            csrf_token=data.csrf_token,
            human_check_token=data.turnstile_token,
            trace=trace,
        )
    )

    if isinstance(outcome, MfaChallengeIssued):
        return _challenge(outcome.pending_token, settings)

    if not isinstance(outcome, SessionIssued):
        return Redirect("/login?error=1", status_code=303)

    # Any session the browser was already carrying ends here, so a login never
    # extends the lifetime of the cookie it replaced.
    await login.logout(request.cookies.get(settings.session_cookie), trace)

    return _signed_in(outcome, settings)


@get("/login/mfa")
async def login_mfa_get(
    request: Request,
    settings: NamedDependency[PortalSettings],
    error: FromQuery[str] = "",
) -> Response:
    if not request.cookies.get(settings.pending_mfa_cookie):
        return Redirect("/login", status_code=303)

    return render("MfaChallenge", user=None, error=error)


@post("/login/mfa", guards=[require_same_origin], status_code=200)
async def login_mfa_post(
    request: Request,
    login: NamedDependency[LoginService],
    settings: NamedDependency[PortalSettings],
    trace: NamedDependency[RequestTrace],
    data: Annotated[MfaForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    outcome = await login.complete_mfa(
        MfaAttempt(
            pending_token=request.cookies.get(settings.pending_mfa_cookie, ""),
            code=data.code,
            trace=trace,
        )
    )

    if not isinstance(outcome, SessionIssued):
        # The pending token was spent by the attempt, so a wrong code sends the
        # browser back to the password step rather than handing it another
        # guess. That bounds guesses at one per password verification.
        return _signed_out("/login?error=1", settings)

    return _signed_in(outcome, settings)


@post("/login/passkey/options", guards=[require_same_origin], status_code=200)
async def login_passkey_options_post(
    request: Request,
    login: NamedDependency[LoginService],
    settings: NamedDependency[PortalSettings],
) -> Response:
    """Called from /login (no pending_mfa cookie: discoverable/passwordless)
    or from /login/mfa (cookie present: a passkey offered instead of a TOTP
    code). Either way the challenge itself, not a form CSRF token, is what
    makes the follow-up /login/passkey/verify call single-use."""
    challenge = await login.begin_passkey_login(
        request.cookies.get(settings.pending_mfa_cookie)
    )

    return Response(
        content={
            "loginToken": challenge.login_token,
            "options": json.loads(challenge.options_json),
        },
        media_type="application/json",
    )


@post("/login/passkey/verify", guards=[require_same_origin], status_code=200)
async def login_passkey_verify_post(
    request: Request,
    login: NamedDependency[LoginService],
    settings: NamedDependency[PortalSettings],
    trace: NamedDependency[RequestTrace],
    data: Annotated[PasskeyLoginForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    outcome = await login.complete_passkey_login(
        PasskeyLoginAttempt(
            login_token=data.login_token,
            response_json=data.response,
            trace=trace,
        )
    )

    # Same bound as login_mfa_post: a failed guess costs the whole pending
    # state, not just this one attempt.
    if not isinstance(outcome, SessionIssued):
        return _signed_out("/login?error=1", settings)

    await login.logout(request.cookies.get(settings.session_cookie), trace)

    return _signed_in(outcome, settings)


@post("/logout", status_code=200)
async def logout_post(
    request: Request,
    login: NamedDependency[LoginService],
    settings: NamedDependency[PortalSettings],
    trace: NamedDependency[RequestTrace],
    data: Annotated[LogoutForm, Body(media_type=RequestEncodingType.URL_ENCODED)],
) -> Response:
    await require_verified_session(request, settings, data.csrf_token)
    await login.logout(request.cookies.get(settings.session_cookie), trace)

    return _signed_out("/login", settings)


def _challenge(pending_token: str, settings: PortalSettings) -> Response:
    response = Redirect("/login/mfa", status_code=303)
    _set_cookie(response, settings.pending_mfa_cookie, pending_token, settings)

    return response


def _signed_in(outcome: SessionIssued, settings: PortalSettings) -> Response:
    destination = "/security" if outcome.needs_setup else "/"
    response = Redirect(destination, status_code=303)
    _set_cookie(response, settings.session_cookie, outcome.cookie_token, settings)
    _clear_cookie(response, settings.pending_mfa_cookie, settings)

    return response


def _signed_out(location: str, settings: PortalSettings) -> Response:
    response = Redirect(location, status_code=303)
    _clear_cookie(response, settings.session_cookie, settings)
    _clear_cookie(response, settings.pending_mfa_cookie, settings)

    return response


def _clear_cookie(response: Response, name: str, settings: PortalSettings) -> None:
    # An expiring cookie has to carry the same attributes as the one it
    # replaces: a __Host- cookie sent without Secure is rejected outright, and
    # the stale one would survive.
    _set_cookie(response, name, "", settings, max_age=0)


def _set_cookie(
    response: Response,
    name: str,
    value: str,
    settings: PortalSettings,
    max_age: int | None = None,
) -> None:
    response.set_cookie(
        name,
        value,
        path="/",
        max_age=max_age,
        secure=settings.serves_https,
        httponly=True,
        samesite="strict",
    )


handlers = (
    login_get,
    login_post,
    login_mfa_get,
    login_mfa_post,
    login_passkey_options_post,
    login_passkey_verify_post,
    logout_post,
)
