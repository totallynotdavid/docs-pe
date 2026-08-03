from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.datastructures import MutableHeaders


if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


# Every script, style and font the portal serves comes from its own origin, so the
# policy needs no exception and injected markup has nowhere to fetch code from. This
# is what forces htmx to be vendored into `web/static`.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)


class SecurityHeaders:
    """Stamp the content security policy onto every response.

    Written against raw ASGI rather than `BaseHTTPMiddleware` so that the job
    progress stream keeps streaming and keeps seeing client disconnects.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_policy(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)["content-security-policy"] = (
                    CONTENT_SECURITY_POLICY
                )
            await send(message)

        await self._app(scope, receive, send_with_policy)
