from __future__ import annotations

import httpx
import pytest

from core.proxy.base import ProxySession


@pytest.mark.parametrize(
    ("username", "password"),
    [
        ("user", "pass"),
        ("user-session-s1_ab", "p@ss:w/rd#1"),
        ("us er", "100%?&=+"),
        ("naïve", "pässwörd"),
    ],
    ids=["plain", "reserved-characters", "space-and-percent", "non-ascii"],
)
def test_proxy_url_carries_credentials_the_http_client_decodes(
    username: str, password: str
) -> None:
    session = ProxySession(
        proxy_id="p1",
        host="proxy.test",
        port="8080",
        username=username,
        password=password,
        session_id="s1",
    )

    proxy = httpx.Proxy(session.as_http_proxy_url())

    assert proxy.url.host == "proxy.test"
    assert proxy.url.port == 8080
    assert proxy.auth == (username, password)
