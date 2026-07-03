from __future__ import annotations

import ssl

from typing import TYPE_CHECKING

import httpx

from robot.domain.errors import TransientTransportError


if TYPE_CHECKING:
    from httpx import Request, Response


class _NormalizingTransport(httpx.AsyncBaseTransport):
    """Wraps the real transport and maps every transport-layer fault to one type.

    httpx maps most httpcore failures to httpx.HTTPError, but a raw ssl.SSLError
    (record-layer fault on a flaky proxy exit) and some OSErrors leak through
    unmapped. Catching them at every call site was whack-a-mole that took down the
    run each time a new one leaked. Normalizing here gives transport faults a
    single owner: any caller using this transport only ever sees
    TransientTransportError, which the retry policy already knows how to handle.
    """

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
