from __future__ import annotations

import urllib.parse

from typing import TYPE_CHECKING

import httpx
import pytest

from fetch.domain.errors import (
    BanSignalError,
    ParseError,
    ProviderSchemaError,
    TransientTransportError,
    UpstreamNotReadyError,
)
from fetch.domain.types import Doc
from fetch.sites.osiptel import site as osiptel_site
from fetch.sites.osiptel.site import OSIPTEL, _counts


if TYPE_CHECKING:
    from collections.abc import Callable


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _row(modalidad: str, numero: str, operador: str) -> dict[str, str]:
    return {"modalidad": modalidad, "numeroServicio": numero, "operador": operador}


async def test_returns_one_row_per_line() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "iTotalRecords": 2,
                "data": [
                    _row("PREPAGO", "96222****", "CLARO"),
                    _row("POSTPAGO", "94915****", "ENTEL"),
                ],
            },
        )

    async with _client(handler) as client:
        rows = await OSIPTEL.lookup(client, Doc("20100000001"))
    assert rows == (
        ("PREPAGO", "96222****", "CLARO"),
        ("POSTPAGO", "94915****", "ENTEL"),
    )


async def test_sends_id_tipo_doc_by_document_kind() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        form = urllib.parse.parse_qs(request.content.decode())
        seen[form["NumeroDocumento"][0]] = form["IdTipoDoc"][0]
        return httpx.Response(200, json={"iTotalRecords": 0, "data": []})

    async with _client(handler) as client:
        await OSIPTEL.lookup(client, Doc("42953322"))
        await OSIPTEL.lookup(client, Doc("20100000001"))
    assert seen == {"42953322": "1", "20100000001": "2"}


async def test_paginates_until_every_row_is_seen() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        start = urllib.parse.parse_qs(request.content.decode())["start"][0]
        if start == "0":
            return httpx.Response(
                200,
                json={
                    "iTotalRecords": 3,
                    "data": [
                        _row("PREPAGO", "96222****", "CLARO"),
                        _row("PREPAGO", "96223****", "CLARO"),
                    ],
                },
            )
        return httpx.Response(
            200,
            json={"iTotalRecords": 3, "data": [_row("POSTPAGO", "90305****", "ENTEL")]},
        )

    async with _client(handler) as client:
        rows = await OSIPTEL.lookup(client, Doc("20100000001"))
    assert rows == (
        ("PREPAGO", "96222****", "CLARO"),
        ("PREPAGO", "96223****", "CLARO"),
        ("POSTPAGO", "90305****", "ENTEL"),
    )


async def test_returns_empty_when_the_doc_has_no_lines() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"iTotalRecords": 0, "data": []})

    async with _client(handler) as client:
        assert await OSIPTEL.lookup(client, Doc("20100000001")) == ()


def test_counts_projection_folds_lines_into_per_carrier_totals() -> None:
    rows = (
        ("PREPAGO", "96222****", "CLARO"),
        ("PREPAGO", "96223****", "CLARO"),
        ("POSTPAGO", "90305****", "ENTEL"),
    )
    assert _counts(rows) == (("CLARO", 2, 3), ("ENTEL", 1, 3))


def test_counts_projection_of_no_lines_is_empty() -> None:
    assert _counts(()) == ()


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (502, TransientTransportError),
        (500, BanSignalError),
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
            await OSIPTEL.lookup(client, Doc("20100000001"))


async def test_a_non_json_body_is_a_parse_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    async with _client(handler) as client:
        with pytest.raises(ParseError):
            await OSIPTEL.lookup(client, Doc("20100000001"))


async def test_a_rejected_request_surfaces_as_a_schema_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"estado": True})

    async with _client(handler) as client:
        with pytest.raises(ProviderSchemaError):
            await OSIPTEL.lookup(client, Doc("20100000001"))


async def test_ready_returns_once_the_success_marker_is_seen() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="...Checa tus lineas...")

    async with _client(handler) as client:
        await OSIPTEL.ready(client, OSIPTEL)


async def test_ready_raises_a_ban_signal_on_a_waf_block() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="The URL you requested has been blocked")

    async with _client(handler) as client:
        with pytest.raises(BanSignalError):
            await OSIPTEL.ready(client, OSIPTEL)


async def test_ready_retries_past_a_transient_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(osiptel_site, "_READY_POLL_S", 0.0)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            msg = "refused"
            raise httpx.ConnectError(msg)
        return httpx.Response(200, text="Checa tus lineas")

    async with _client(handler) as client:
        await OSIPTEL.ready(client, OSIPTEL)
    assert attempts["n"] == 2


async def test_ready_gives_up_after_the_deadline_with_no_success_or_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(osiptel_site, "_READY_TIMEOUT_S", 0.05)
    monkeypatch.setattr(osiptel_site, "_READY_POLL_S", 0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="still loading")

    async with _client(handler) as client:
        with pytest.raises(UpstreamNotReadyError):
            await OSIPTEL.ready(client, OSIPTEL)
