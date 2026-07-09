from __future__ import annotations

import ssl

import httpx
import pytest

from robot.domain.errors import TransientTransportError
from robot.proxy.transport import _NormalizingTransport


class _RaisingTransport(httpx.AsyncBaseTransport):
    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise self._exc

    async def aclose(self) -> None:
        return None


class _EchoTransport(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    async def aclose(self) -> None:
        return None


@pytest.mark.parametrize(
    "exc",
    [
        ssl.SSLError("record layer failure"),
        OSError("connection reset"),
        httpx.ConnectError("refused"),
    ],
)
async def test_every_transport_fault_normalizes_to_transient(
    exc: BaseException,
) -> None:
    # SSLError and bare OSError leak past httpx's own mapping.
    transport = _NormalizingTransport(_RaisingTransport(exc))
    request = httpx.Request("GET", "https://example.test/")
    with pytest.raises(TransientTransportError):
        await transport.handle_async_request(request)


async def test_a_successful_response_passes_through_untouched() -> None:
    transport = _NormalizingTransport(_EchoTransport())
    request = httpx.Request("GET", "https://example.test/")
    response = await transport.handle_async_request(request)
    assert response.status_code == 200
