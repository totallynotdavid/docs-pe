from __future__ import annotations

import urllib.parse

from typing import TYPE_CHECKING

import httpx
import pytest

from fetch.domain.errors import (
    BanSignalError,
    RucNotFoundError,
    TransientTransportError,
)
from fetch.domain.types import Doc
from fetch.sites.sunat.site import SUNAT, SUNAT_REPS


if TYPE_CHECKING:
    from collections.abc import Callable


_RESULT_HTML = (
    "<html><body><h2>Resultado de la Búsqueda</h2>"
    "<h4>Tipo de Documento:</h4><p>Doc  20100000001  - ACME SAC</p>"
    "</body></html>"
)

_IDENTITY_JSON = {"lista": [{"apenomdenunciado": "ACME SAC"}]}
_IDENTITY_NOT_FOUND_JSON = {"error": "no existe"}

_REPS_HTML = (
    "<html><body><table><tbody>"
    "<tr><td>DNI</td><td>12345678</td><td>JUAN PEREZ</td>"
    "<td>GERENTE GENERAL</td><td>01/01/2020</td></tr>"
    "</tbody></table></body></html>"
)
_REPS_EMPTY_HTML = "<html><body><tbody></tbody></body></html>"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_returns_the_document_record() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_RESULT_HTML)

    async with _client(handler) as client:
        rows = await SUNAT.lookup(client, Doc("20100000001"))
    assert rows == (("Doc", "20100000001", "ACME SAC", ""),)


async def test_returns_empty_for_a_result_page_without_a_document_row() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>Resultado de la Búsqueda</html>")

    async with _client(handler) as client:
        assert await SUNAT.lookup(client, Doc("20100000001")) == ()


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
            await SUNAT.lookup(client, Doc("20100000001"))


async def test_ready_passes_on_a_healthy_home_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    async with _client(handler) as client:
        await SUNAT.ready(client, SUNAT)


async def test_ready_flags_a_dead_exit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="down")

    async with _client(handler) as client:
        with pytest.raises(BanSignalError):
            await SUNAT.ready(client, SUNAT)


async def test_reps_chains_identity_into_getrepleg() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_IDENTITY_JSON)
        return httpx.Response(200, text=_REPS_HTML)

    async with _client(handler) as client:
        rows = await SUNAT_REPS.lookup(client, Doc("20100000001"))
    assert rows == (
        ("ACME SAC", "DNI", "12345678", "JUAN PEREZ", "GERENTE GENERAL", "01/01/2020"),
    )


async def test_reps_posts_an_empty_des_ruc_in_the_reps_request() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_IDENTITY_JSON)
        captured["body"] = request.content.decode()
        return httpx.Response(200, text=_REPS_HTML)

    async with _client(handler) as client:
        await SUNAT_REPS.lookup(client, Doc("20100000001"))

    body = urllib.parse.parse_qs(captured["body"], keep_blank_values=True)
    assert body["desRuc"][0] == ""


async def test_reps_returns_empty_when_the_company_has_no_representatives() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_IDENTITY_JSON)
        return httpx.Response(200, text=_REPS_EMPTY_HTML)

    async with _client(handler) as client:
        assert await SUNAT_REPS.lookup(client, Doc("20100000001")) == ()


async def test_reps_raises_not_found_and_skips_the_reps_request() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(200, json=_IDENTITY_NOT_FOUND_JSON)

    async with _client(handler) as client:
        with pytest.raises(RucNotFoundError):
            await SUNAT_REPS.lookup(client, Doc("20100000001"))
    assert calls == ["GET"]


async def test_reps_maps_a_fault_on_the_identity_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="nope")

    async with _client(handler) as client:
        with pytest.raises(BanSignalError):
            await SUNAT_REPS.lookup(client, Doc("20100000001"))


async def test_reps_maps_a_fault_on_the_reps_request_after_identity_succeeds() -> None:
    # A fault on the second request must not be swallowed just because the first
    # succeeded.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_IDENTITY_JSON)
        return httpx.Response(500, text="nope")

    async with _client(handler) as client:
        with pytest.raises(BanSignalError):
            await SUNAT_REPS.lookup(client, Doc("20100000001"))
