from __future__ import annotations

import http.client
import threading

from typing import TYPE_CHECKING

import pytest

from capture.relay import RelayState, build_server
from capture.ruc import RUC
from capture.sites.entel.site import ENTEL
from capture.store import ObservationStore


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


TOKEN = "relay-token"
RUC_VALUE = "20100000092"


@pytest.fixture
def relay_address(tmp_path: Path) -> Iterator[tuple[str, int]]:
    with ObservationStore(tmp_path / "capture.sqlite3") as store:
        state = RelayState(
            rucs=[RUC(RUC_VALUE)],
            store=store,
            run_id="run-1",
            token=TOKEN,
            site=ENTEL,
        )
        server = build_server(host="127.0.0.1", port=0, state=state)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()

        try:
            host, port = server.server_address[:2]
            yield str(host), int(port)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


@pytest.mark.parametrize(
    ("token", "status"),
    [
        (TOKEN, 200),
        ("wrong-token", 401),
        ("", 401),
        ("señal", 401),
        ("ÿþ", 401),
    ],
    ids=["valid", "wrong", "empty", "latin-1", "high-bytes"],
)
def test_next_answers_only_a_request_with_the_relay_token(
    relay_address: tuple[str, int], token: str, status: int
) -> None:
    host, port = relay_address
    connection = http.client.HTTPConnection(host, port, timeout=5)

    try:
        connection.putrequest("GET", "/next")
        connection.putheader("Origin", ENTEL.origin)
        connection.putheader("X-Capture-Token", token)
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()

    assert response.status == status
    if status == 200:
        assert RUC_VALUE.encode() in body
