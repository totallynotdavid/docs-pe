from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from robot.domain.errors import BanSignalError, TransientTransportError
from robot.domain.types import RUC
from robot.sites.sunat.site import SUNAT


if TYPE_CHECKING:
    from collections.abc import Callable


_RESULT_HTML = (
    "<html><body><h2>Resultado de la Búsqueda</h2>"
    "<h4>Tipo de Documento:</h4><p>RUC  20100000001  - ACME SAC</p>"
    "</body></html>"
)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_returns_the_document_record() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_RESULT_HTML)

    async with _client(handler) as client:
        rows = await SUNAT.lookup(client, RUC("20100000001"))
    assert rows == (("RUC", "20100000001", "ACME SAC"),)


async def test_returns_empty_for_a_result_page_without_a_document_row() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>Resultado de la Búsqueda</html>")

    async with _client(handler) as client:
        assert await SUNAT.lookup(client, RUC("20100000001")) == ()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (503, TransientTransportError),
        (500, BanSignalError),
        (403, BanSignalError),
        (302, TransientTransportError),
    ],
)
async def test_maps_http_status_to_the_right_fault(
    status: int, expected: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="nope")

    async with _client(handler) as client:
        with pytest.raises(expected):
            await SUNAT.lookup(client, RUC("20100000001"))


async def test_ready_passes_on_a_healthy_home_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    async with _client(handler) as client:
        await SUNAT.ready(client)


async def test_ready_flags_a_dead_exit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    async with _client(handler) as client:
        with pytest.raises(BanSignalError):
            await SUNAT.ready(client)
