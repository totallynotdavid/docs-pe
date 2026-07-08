from __future__ import annotations

import urllib.parse

from typing import TYPE_CHECKING

import httpx
import pytest

from robot.domain.errors import (
    BanSignalError,
    RucNotFoundError,
    TransientTransportError,
)
from robot.domain.types import RUC
from robot.sites.sunat.site import SUNAT, SUNAT_REPS


if TYPE_CHECKING:
    from collections.abc import Callable


_RESULT_HTML = (
    "<html><body><h2>Resultado de la Búsqueda</h2>"
    "<h4>Tipo de Documento:</h4><p>RUC  20100000001  - ACME SAC</p>"
    "</body></html>"
)

_FICHA_HTML = (
    "<html><body><h4>N&uacute;mero de RUC:</h4>"
    "<h4>20100000001 - ACME SAC</h4></body></html>"
)

_RUC_NOT_FOUND_HTML = (
    '<html><body><div class="panel-body text-center">'
    "<strong>  </strong></div></body></html>"
)

_REPS_HTML = (
    "<html><body><table><tbody>"
    "<tr><td>DNI</td><td>12345678</td><td>JUAN PEREZ</td>"
    "<td>GERENTE GENERAL</td><td>01/01/2020</td></tr>"
    "</tbody></table></body></html>"
)


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _accion(request: httpx.Request) -> str:
    return urllib.parse.parse_qs(request.content.decode())["accion"][0]


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


async def test_reps_chains_consulta_razon_social_into_getrepleg() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _accion(request) == "consPorRuc":
            return httpx.Response(200, text=_FICHA_HTML)
        body = urllib.parse.parse_qs(request.content.decode())
        assert body["desRuc"][0] == "ACME SAC"
        return httpx.Response(200, text=_REPS_HTML)

    async with _client(handler) as client:
        rows = await SUNAT_REPS.lookup(client, RUC("20100000001"))
    assert rows == (
        ("ACME SAC", "DNI", "12345678", "JUAN PEREZ", "GERENTE GENERAL", "01/01/2020"),
    )


async def test_reps_returns_empty_when_the_company_has_no_representatives() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if _accion(request) == "consPorRuc":
            return httpx.Response(200, text=_FICHA_HTML)
        return httpx.Response(200, text="<html><body><tbody></tbody></body></html>")

    async with _client(handler) as client:
        assert await SUNAT_REPS.lookup(client, RUC("20100000001")) == ()


async def test_reps_raises_not_found_and_skips_the_second_request() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(_accion(request))
        return httpx.Response(200, text=_RUC_NOT_FOUND_HTML)

    async with _client(handler) as client:
        with pytest.raises(RucNotFoundError):
            await SUNAT_REPS.lookup(client, RUC("20100000001"))
    assert calls == ["consPorRuc"]


async def test_reps_maps_a_fault_on_the_second_request_too() -> None:
    # _post is shared with the consulta call (already status-mapped above); this
    # proves a fault on the second request in the chain also propagates, rather
    # than being swallowed once the first request has already succeeded.
    def handler(request: httpx.Request) -> httpx.Response:
        if _accion(request) == "consPorRuc":
            return httpx.Response(200, text=_FICHA_HTML)
        return httpx.Response(500, text="nope")

    async with _client(handler) as client:
        with pytest.raises(BanSignalError):
            await SUNAT_REPS.lookup(client, RUC("20100000001"))
