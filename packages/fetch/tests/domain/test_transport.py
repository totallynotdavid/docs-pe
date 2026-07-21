from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from fetch.domain.errors import BanSignalError, TransientTransportError
from fetch.domain.transport import classify_status, raise_for_status, warm_endpoints
from fetch.domain.types import Endpoint


if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (200, None),
        (502, TransientTransportError),
        (503, TransientTransportError),
        (504, TransientTransportError),
        (403, BanSignalError),
        (429, BanSignalError),
        (500, BanSignalError),
        (501, BanSignalError),
        (302, TransientTransportError),
    ],
)
def test_classify_status_maps_every_band(
    status: int, expected: type[Exception] | None
) -> None:
    assert classify_status(status) is expected


def test_raise_for_status_passes_through_a_200() -> None:
    raise_for_status(200, endpoint=Endpoint(name="home", url="https://x.test"))


def test_raise_for_status_labels_a_transient_status_in_the_message() -> None:
    with pytest.raises(TransientTransportError, match="home transient status=503"):
        raise_for_status(503, endpoint=Endpoint(name="home", url="https://x.test"))


def test_raise_for_status_labels_a_failed_status_in_the_message() -> None:
    with pytest.raises(BanSignalError, match="home failed status=403"):
        raise_for_status(403, endpoint=Endpoint(name="home", url="https://x.test"))


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_warm_endpoints_gets_every_endpoint() -> None:
    hit: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hit.append(str(request.url))
        return httpx.Response(200)

    endpoints = (
        Endpoint(name="home", url="https://home.test"),
        Endpoint(name="api", url="https://api.test"),
    )
    async with _client(handler) as client:
        await warm_endpoints(client, endpoints)
    assert hit == ["https://home.test", "https://api.test"]


async def test_warm_endpoints_raises_on_the_first_fault_without_checking_the_rest() -> (
    None
):
    hit: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hit.append(str(request.url))
        return httpx.Response(403)

    endpoints = (
        Endpoint(name="first", url="https://first.test"),
        Endpoint(name="second", url="https://second.test"),
    )
    async with _client(handler) as client:
        with pytest.raises(BanSignalError):
            await warm_endpoints(client, endpoints)
    assert hit == ["https://first.test"]
