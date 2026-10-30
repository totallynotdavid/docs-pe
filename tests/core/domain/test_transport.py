from __future__ import annotations

import pytest

from core.domain.errors import BanSignalError, TransientTransportError
from core.domain.transport import classify_status


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
