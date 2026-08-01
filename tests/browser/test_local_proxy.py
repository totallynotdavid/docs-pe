from __future__ import annotations

import base64
import socket
import socketserver
import threading

from browser.local_proxy import LocalProxy
from browser.proxy import ProxyEndpoint


class _FakeUpstream(socketserver.ThreadingTCPServer):
    """Accepts one CONNECT, records its auth header, then echoes the tunnel."""

    daemon_threads = True
    allow_reuse_address = True
    seen_auth: str = ""
    seen_authority: str = ""


class _FakeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _FakeUpstream)
        sock = self.request
        assert isinstance(sock, socket.socket)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                return
            head += chunk
        lines = head.decode().split("\r\n")
        server.seen_authority = lines[0].split(" ")[1]
        for line in lines:
            if line.lower().startswith("proxy-authorization:"):
                server.seen_auth = line.split(":", 1)[1].strip()
        sock.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        # Echo whatever the tunnel carries, so the test can prove it relays.
        while True:
            payload = sock.recv(4096)
            if not payload:
                return
            sock.sendall(payload)


def _connect_through(address: str, authority: str) -> tuple[bytes, socket.socket]:
    host, port = address.split(":")
    client = socket.create_connection((host, int(port)), timeout=5)
    client.sendall(
        f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode()
    )
    return client.recv(4096), client


def test_relay_authenticates_upstream_and_tunnels_bytes() -> None:
    upstream = _FakeUpstream(("127.0.0.1", 0), _FakeHandler)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = upstream.server_address[0], upstream.server_address[1]
        endpoint = ProxyEndpoint(
            host=str(host), port=str(port), username="user-session-1", password="secret"
        )
        with LocalProxy(endpoint) as address:
            # Chrome connects with no credentials at all.
            response, client = _connect_through(address, "example.com:443")
            assert b"200" in response
            client.sendall(b"ping")
            assert client.recv(4096) == b"ping"
            client.close()
    finally:
        upstream.shutdown()
        upstream.server_close()
        thread.join(timeout=5)

    expected = base64.b64encode(b"user-session-1:secret").decode()
    assert upstream.seen_auth == f"Basic {expected}"
    assert upstream.seen_authority == "example.com:443"


def test_relay_reports_upstream_refusal() -> None:
    # No upstream listening: the relay must answer rather than hang.
    endpoint = ProxyEndpoint(host="127.0.0.1", port="9", username="u", password="p")
    with LocalProxy(endpoint) as address:
        response, client = _connect_through(address, "example.com:443")
        client.close()
    assert b"502" in response
