from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.datastructures import MutableHeaders


if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send


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
                MutableHeaders(scope=message)["content-security-policy"] = (
                    CONTENT_SECURITY_POLICY
                )
            await send(message)

        await self._app(scope, receive, send_with_policy)
