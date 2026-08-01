from __future__ import annotations

import pytest

from capture.errors import CaptureError, RejectedError
from capture.sites.entel.parse import parse_lookup_result


RUC = "20100000092"


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "ruc": RUC,
        "hasError": False,
        "debt": {
            "DocumentNumber": RUC,
            "DebtTotal": "726.91",
            "HasPunishment": False,
            "Accounts": {"List": [{"Account": "sample"}]},
        },
        "elapsedMs": 900,
        "mintMs": 300,
    }
    payload.update(overrides)
    return payload


def test_parses_a_verified_debt_response() -> None:
    result = parse_lookup_result(_payload(), expected_ruc=RUC)
    assert result.columns == {"debt_total": "726.91", "has_punishment": "False"}
    assert result.elapsed_ms == 900


def test_rejected_shape_is_not_misread_as_zero_debt() -> None:
    with pytest.raises(RejectedError):
        parse_lookup_result(_payload(hasError=True), expected_ruc=RUC)


def test_script_exception_is_a_failure() -> None:
    with pytest.raises(CaptureError, match="script failed"):
        parse_lookup_result({"ruc": RUC, "exception": "boom"}, expected_ruc=RUC)
