from __future__ import annotations

import re

from http.cookies import SimpleCookie
from typing import TYPE_CHECKING

from litestar.datastructures.headers import MutableScopeHeaders

from portal.turnstile import WIDGET_SCRIPT_ORIGIN


if TYPE_CHECKING:
    from litestar.types import (
        ASGIApp,
        HTTPResponseBodyEvent,
        HTTPResponseStartEvent,
        Message,
        Receive,
        Scope,
        Send,
    )

    from portal.settings import PortalSettings


# default-src 'self' would block the Turnstile widget, and a login page whose
# challenge cannot load fails closed forever. Only the two directives the widget
# needs name challenges.cloudflare.com; everything else stays same-origin.
# https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Headers_Cheat_Sheet.html
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    f"script-src 'self' {WIDGET_SCRIPT_ORIGIN}; "
    f"frame-src {WIDGET_SCRIPT_ORIGIN}; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

# No feature here needs a browser permission the portal doesn't already avoid
# asking for; denying all of them means a future embed or dependency cannot
# start requesting camera/mic/location access without the change showing up
# as a diff to this line.
PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"

# Browsers ignore Strict-Transport-Security served over plain http, so this is
# unconditional: it costs nothing on a development origin and cannot be
# forgotten on a real one.
RESPONSE_HEADERS = (
    ("content-security-policy", CONTENT_SECURITY_POLICY),
    ("strict-transport-security", "max-age=63072000; includeSubDomains; preload"),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("permissions-policy", PERMISSIONS_POLICY),
    # frame-ancestors 'none' above already covers this; kept for the clients
    # (pre-2020 or CSP-stripping proxies) that only honor the older header.
    ("x-frame-options", "DENY"),
)


class SecurityHeaders:
    """Add security headers to HTTP responses."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableScopeHeaders.from_message(message)

                for name, value in RESPONSE_HEADERS:
                    headers[name] = value

            await send(message)

        await self._app(scope, receive, send_with_security_headers)


class HTTPSRedirect:
    """Redirect plain HTTP requests to HTTPS."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["scheme"] == "https":
            await self._app(scope, receive, send)
            return

        headers = MutableScopeHeaders(scope)
        host = headers.get("host", "")
        query_string = scope["query_string"].decode("latin-1")

        target = f"https://{host}{scope['path']}"
        if query_string:
            target = f"{target}?{query_string}"

        start: HTTPResponseStartEvent = {
            "type": "http.response.start",
            "status": 307,
            "headers": [(b"location", target.encode("latin-1"))],
        }
        body: HTTPResponseBodyEvent = {
            "type": "http.response.body",
            "body": b"",
            "more_body": False,
        }

        await send(start)
        await send(body)


_TEAM_PATH = re.compile(r"^/teams/([0-9a-fA-F-]{36})(?:/|$)")

LAST_TEAM_COOKIE_MAX_AGE = 180 * 24 * 60 * 60


class RememberLastTeam:
    """Sets a last-visited-team cookie on every successful /teams/{id}/...
    response, so dashboard() can skip the team picker next visit the same way
    it already does when there is only one team. Read-only convenience, never
    a source of authorization: every team route still checks membership on
    its own.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        match = _TEAM_PATH.match(scope["path"])

        if match is None:
            await self._app(scope, receive, send)
            return

        team_id = match.group(1)

        async def send_with_cookie(message: Message) -> None:
            if message["type"] == "http.response.start" and message["status"] < 400:
                settings: PortalSettings = scope["app"].state.settings
                headers = MutableScopeHeaders.from_message(message)
                headers.add(
                    "set-cookie",
                    _last_team_cookie_header(team_id, settings),
                )

            await send(message)

        await self._app(scope, receive, send_with_cookie)


def _last_team_cookie_header(team_id: str, settings: PortalSettings) -> str:
    cookie: SimpleCookie = SimpleCookie()
    name = settings.last_team_cookie
    cookie[name] = team_id
    cookie[name]["path"] = "/"
    cookie[name]["max-age"] = LAST_TEAM_COOKIE_MAX_AGE
    cookie[name]["httponly"] = True
    cookie[name]["samesite"] = "Strict"

    if settings.serves_https:
        cookie[name]["secure"] = True

    return cookie[name].OutputString()
