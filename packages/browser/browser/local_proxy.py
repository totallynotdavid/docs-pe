from __future__ import annotations

import base64
import contextlib
import selectors
import socket
import socketserver
import threading

from typing import TYPE_CHECKING, cast


if TYPE_CHECKING:
    from types import TracebackType

    from browser.proxy import ProxyEndpoint


# SeleniumBase's Pure CDP mode authenticates an upstream proxy by enabling CDP
# Fetch interception (set_auth -> Fetch.enable with handle_auth_requests): every
# request is paused and resumed through a Python handler on the CDP event loop.
# One simple request survives that, but a heavy SPA (Entel's OutSystems app)
# stalls and never renders, because the interception starves its subresource
# XHRs. This is not a Chrome-version regression to downgrade around: the Fetch
# path is unconditional (no version gate), and the blank-render stall reproduces
# on Chrome 147, 148, 149, and 150 alike (measured). This relay terminates the
# auth locally instead: Chrome talks to an unauthenticated 127.0.0.1 proxy, so no
# interception is ever enabled, and we attach the upstream credentials ourselves.

_BUFFER_BYTES = 65536
_MAX_HEAD_BYTES = 65536
_UPSTREAM_TIMEOUT_S = 30.0


class LocalProxy:
    """An unauthenticated local proxy that forwards to an authenticated one.

    Yields the "host:port" to hand Chrome. One relay per browser session, so a
    session restart rotates the upstream exit exactly as before.
    """

    def __init__(self, endpoint: ProxyEndpoint) -> None:
        self._endpoint = endpoint
        self._server: _RelayServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> str:
        server = _RelayServer(self._endpoint)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        address = cast("tuple[str, int]", server.server_address)
        return f"{address[0]}:{address[1]}"

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


class _RelayServer(socketserver.ThreadingTCPServer):
    # Chrome opens many parallel connections; daemon threads keep teardown from
    # blocking on an idle tunnel.
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, endpoint: ProxyEndpoint) -> None:
        credentials = f"{endpoint.username}:{endpoint.password}".encode()
        self.upstream = (endpoint.host, int(endpoint.port))
        self.auth_header = (
            b"Proxy-Authorization: Basic " + base64.b64encode(credentials) + b"\r\n"
        )
        # Port 0 lets the OS pick a free port, so concurrent runs never collide.
        super().__init__(("127.0.0.1", 0), _RelayHandler)


class _RelayHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = cast("_RelayServer", self.server)
        client = cast("socket.socket", self.request)
        head = _read_head(client)
        if not head:
            return
        request_line = head.partition(b"\r\n")[0]
        method, _, remainder = request_line.partition(b" ")
        try:
            upstream = socket.create_connection(
                server.upstream, timeout=_UPSTREAM_TIMEOUT_S
            )
        except OSError:
            with contextlib.suppress(OSError):
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        try:
            if method.upper() == b"CONNECT":
                authority = remainder.partition(b" ")[0]
                _tunnel(client, upstream, authority, server.auth_header)
            else:
                _forward(client, upstream, head, server.auth_header)
        except OSError:
            # A dropped client or upstream connection is routine, not an error.
            pass
        finally:
            upstream.close()


def _tunnel(
    client: socket.socket,
    upstream: socket.socket,
    authority: bytes,
    auth_header: bytes,
) -> None:
    upstream.sendall(
        b"CONNECT " + authority + b" HTTP/1.1\r\n"
        b"Host: " + authority + b"\r\n" + auth_header + b"\r\n"
    )
    response = _read_head(upstream)
    if b" 200 " not in response.partition(b"\r\n")[0]:
        # Surface the upstream refusal (bad credentials, blocked host) verbatim.
        client.sendall(response or b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        return
    client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
    _pipe(client, upstream)


def _forward(
    client: socket.socket,
    upstream: socket.socket,
    head: bytes,
    auth_header: bytes,
) -> None:
    # Plain HTTP: the request already carries an absolute URI, so it only needs
    # the credentials inserted after the request line.
    request_line, _, rest = head.partition(b"\r\n")
    upstream.sendall(request_line + b"\r\n" + auth_header + rest)
    _pipe(client, upstream)


def _read_head(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data and len(data) < _MAX_HEAD_BYTES:
        chunk = sock.recv(_BUFFER_BYTES)
        if not chunk:
            break
        data += chunk
    return data


def _pipe(first: socket.socket, second: socket.socket) -> None:
    # Both directions stream until each side has signalled EOF.
    first.settimeout(None)
    second.settimeout(None)
    with selectors.DefaultSelector() as selector:
        selector.register(first, selectors.EVENT_READ)
        selector.register(second, selectors.EVENT_READ)
        open_sides = 2
        while open_sides:
            for key, _mask in selector.select():
                source = cast("socket.socket", key.fileobj)
                target = second if source is first else first
                chunk = source.recv(_BUFFER_BYTES)
                if not chunk:
                    selector.unregister(source)
                    open_sides -= 1
                    with contextlib.suppress(OSError):
                        target.shutdown(socket.SHUT_WR)
                    continue
                target.sendall(chunk)
