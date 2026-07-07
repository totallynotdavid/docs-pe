from __future__ import annotations

import urllib.parse

from typing import TYPE_CHECKING

import httpx
import pytest

from robot.domain.errors import (
    BanSignalError,
    ParseError,
    ProviderSchemaError,
    TransientTransportError,
)
from robot.domain.types import RUC
from robot.sites.osiptel.site import OSIPTEL


if TYPE_CHECKING:
    from collections.abc import Callable


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_aggregates_a_single_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "iTotalRecords": 2,
                "data": [{"operador": "CLARO"}, {"operador": "MOVISTAR"}],
            },
        )

    async with _client(handler) as client:
        rows = await OSIPTEL.lookup(client, RUC("20100000001"))
    assert rows == (("CLARO", 1, 2), ("MOVISTAR", 1, 2))


async def test_paginates_until_every_row_is_seen() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        start = urllib.parse.parse_qs(request.content.decode())["start"][0]
        if start == "0":
            return httpx.Response(
                200,
                json={
                    "iTotalRecords": 3,
                    "data": [{"operador": "CLARO"}, {"operador": "CLARO"}],
                },
            )
        return httpx.Response(
            200, json={"iTotalRecords": 3, "data": [{"operador": "ENTEL"}]}
        )

    async with _client(handler) as client:
        rows = await OSIPTEL.lookup(client, RUC("20100000001"))
    assert rows == (("CLARO", 2, 3), ("ENTEL", 1, 3))


async def test_returns_empty_when_the_ruc_has_no_lines() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"iTotalRecords": 0, "data": []})

    async with _client(handler) as client:
        assert await OSIPTEL.lookup(client, RUC("20100000001")) == ()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (502, TransientTransportError),
        (503, TransientTransportError),
        (500, BanSignalError),
        (403, BanSignalError),
        (429, BanSignalError),
        (418, TransientTransportError),
    ],
)
async def test_maps_http_status_to_the_right_fault(
    status: int, expected: type[Exception]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="nope")

    async with _client(handler) as client:
        with pytest.raises(expected):
            await OSIPTEL.lookup(client, RUC("20100000001"))


async def test_a_non_json_body_is_a_parse_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    async with _client(handler) as client:
        with pytest.raises(ParseError):
            await OSIPTEL.lookup(client, RUC("20100000001"))


async def test_a_rejected_request_surfaces_as_a_schema_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"estado": True})

    async with _client(handler) as client:
        with pytest.raises(ProviderSchemaError):
            await OSIPTEL.lookup(client, RUC("20100000001"))
