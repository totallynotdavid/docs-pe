from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest

from fetch.proxy import egress as egress_mod
from fetch.proxy.base import ProxySession
from fetch.proxy.egress import _extract_ip, _is_valid_ip, resolve_egress_ip
from fetch.proxy.transport import _NormalizingTransport


if TYPE_CHECKING:
    from collections.abc import Callable


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ("not-a-dict", ""),
        ({}, ""),
        ({"query": "1.2.3.4"}, "1.2.3.4"),
        ({"ip": "5.6.7.8"}, "5.6.7.8"),
        ({"origin": "9.9.9.9, 10.10.10.10"}, "9.9.9.9"),
        ({"query": "not-an-ip"}, ""),
        ({"query": "not-an-ip", "ip": "5.6.7.8"}, "5.6.7.8"),
    ],
)
def test_extract_ip(payload: object, expected: str) -> None:
    assert _extract_ip(payload) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1.2.3.4", True),
        ("not-an-ip", False),
    ],
)
def test_is_valid_ip(value: str, *, expected: bool) -> None:
    assert _is_valid_ip(value) is expected


def _fake_session() -> ProxySession:
    return ProxySession(
        proxy_id="p1",
        host="proxy.test",
        port="1",
        username="u",
        password="p",
        session_id="s1",
    )


def _patch_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    def fake_build_transport(*, proxy_url: str) -> httpx.AsyncBaseTransport:
        return _NormalizingTransport(httpx.MockTransport(handler))

    monkeypatch.setattr(egress_mod, "build_transport", fake_build_transport)


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(egress_mod.asyncio, "sleep", fake_sleep)


async def test_returns_the_first_successful_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500)
        return httpx.Response(200, json={"query": "1.2.3.4"})

    _patch_transport(monkeypatch, handler)
    assert await resolve_egress_ip(_fake_session()) == "1.2.3.4"
    assert calls["n"] == 2


async def test_retries_a_second_round_when_every_url_fails_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 3:
            return httpx.Response(500)
        return httpx.Response(200, json={"ip": "5.6.7.8"})

    _patch_transport(monkeypatch, handler)
    assert await resolve_egress_ip(_fake_session()) == "5.6.7.8"
    assert calls["n"] == 4


async def test_gives_up_and_returns_empty_after_three_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _patch_transport(monkeypatch, handler)
    assert await resolve_egress_ip(_fake_session()) == ""
