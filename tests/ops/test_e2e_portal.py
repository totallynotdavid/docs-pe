from __future__ import annotations

import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

import httpx
import pytest

from ops.e2e_portal import dokploy_session, portal_client


if TYPE_CHECKING:
    from collections.abc import Iterator


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/login":
            self.send_response(303)
            self.send_header("Set-Cookie", "session=abc; Path=/")
            self.send_header("Location", "/whoami")
            self.end_headers()
            return

        body = f"{self.headers['Host']} {self.headers.get('Cookie')}".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # ruff: ignore[builtin-argument-shadowing]
        pass


@pytest.fixture
def server() -> Iterator[str]:
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        yield f"127.0.0.1:{httpd.server_address[1]}"
        httpd.shutdown()


def test_the_client_keeps_the_origins_name_and_cookies_while_connecting_elsewhere(
    server: str,
) -> None:
    with portal_client("http://portal.test", server) as client:
        response = client.get("/login", follow_redirects=True)

    assert str(response.url) == "http://portal.test/whoami"
    assert response.text == "portal.test session=abc"


class DokployHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.send_response(200)
        self.send_header("Set-Cookie", "better-auth.session_token=abc.def; Path=/")
        self.send_header("Set-Cookie", "better-auth.dont_remember=x; Path=/")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def do_GET(self) -> None:
        signed_in = "better-auth.session_token=abc.def" in (
            self.headers.get("Cookie") or ""
        )
        self.send_response(200 if signed_in else 401)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"[]")

    def log_message(self, format: str, *args: object) -> None:  # ruff: ignore[builtin-argument-shadowing]
        pass


def test_the_dokploy_session_is_the_cookie_header_the_deploy_tool_sends() -> None:
    with ThreadingHTTPServer(("127.0.0.1", 0), DokployHandler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        with httpx.Client(base_url=url) as client:
            cookie = dokploy_session(client, "admin@example.com", "secret")
        httpd.shutdown()

    assert cookie == "better-auth.session_token=abc.def; better-auth.dont_remember=x"
