from __future__ import annotations

import ssl

from typing import TYPE_CHECKING

import httpx

from fetch.domain.errors import TransientTransportError


if TYPE_CHECKING:
    from httpx import Request, Response


class _NormalizingTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport) -> None:
        self._inner = inner

    async def handle_async_request(self, request: Request) -> Response:
        try:
            return await self._inner.handle_async_request(request)
        except (httpx.HTTPError, ssl.SSLError, OSError) as exc:
            msg = f"proxy transport failed: {type(exc).__name__}: {exc}"
            raise TransientTransportError(msg) from exc

    async def aclose(self) -> None:
        await self._inner.aclose()


def build_transport(*, proxy_url: str) -> httpx.AsyncBaseTransport:
    return _NormalizingTransport(httpx.AsyncHTTPTransport(proxy=proxy_url))
