from __future__ import annotations

from typing import TYPE_CHECKING

from litestar.datastructures.headers import MutableScopeHeaders


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


CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class SecurityHeaders:
    """Raw ASGI middleware so streaming responses and disconnect detection work."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_policy(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableScopeHeaders.from_message(message)["content-security-policy"] = (
                    CONTENT_SECURITY_POLICY
                )
            await send(message)

        await self._app(scope, receive, send_with_policy)


class HTTPSRedirect:
    """Raw ASGI middleware redirecting plain HTTP to HTTPS.

    Only wired in when TLS is not already terminated upstream (see app.py),
    so this only runs where the ASGI server itself faces plaintext traffic.
    """

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
