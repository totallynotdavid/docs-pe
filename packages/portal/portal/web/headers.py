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


# Turnstile and Cloudflare Analytics are the only third-party origins allowed.
# Pin the analytics bootstrap instead of enabling `unsafe-inline`.
CLOUDFLARE_BEACON_BOOTSTRAP_HASH = (
    "'sha256-4+7Ef+Wr2glo3yZvK4WyJZtdEuvLztBwfMaMbPpX4WQ='"
)
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    f"script-src 'self' {WIDGET_SCRIPT_ORIGIN} https://static.cloudflareinsights.com "
    f"{CLOUDFLARE_BEACON_BOOTSTRAP_HASH}; "
    "connect-src 'self' https://cloudflareinsights.com; "
    f"frame-src {WIDGET_SCRIPT_ORIGIN}; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)

PERMISSIONS_POLICY = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"

# Browsers ignore HSTS served over plain HTTP, so send it unconditionally.
RESPONSE_HEADERS = (
    ("content-security-policy", CONTENT_SECURITY_POLICY),
    ("strict-transport-security", "max-age=63072000; includeSubDomains; preload"),
    ("x-content-type-options", "nosniff"),
    ("referrer-policy", "no-referrer"),
    ("permissions-policy", PERMISSIONS_POLICY),
    # Keep legacy protection for clients that do not enforce frame-ancestors.
    ("x-frame-options", "DENY"),
)


class SecurityHeaders:
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
    """dashboard() reads the last-visited-team cookie to skip the team picker,
    as it does when the user has one team. The cookie never authorizes
    anything. A team route reaches team data only through service methods
    whose access decorators check the caller against that team, and several
    of those admit members alone, not a site admin outside the team.
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
