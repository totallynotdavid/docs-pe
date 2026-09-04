from __future__ import annotations

import pytest

from browser.errors import BrowserError, RejectedError
from browser.sites.entel.parse import parse_lookup_result


RUC = "20100000092"
DNI = "07988633"


def _payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "document": RUC,
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
    result = parse_lookup_result(_payload(), expected_document=RUC)
    assert result.subject == RUC
    assert result.columns == {"debt_total": "726.91", "has_punishment": "False"}
    assert result.elapsed_ms == 900
    assert result.mint_ms == 300


def test_parses_a_dni_debt_response() -> None:
    debt = {"DocumentNumber": DNI, "DebtTotal": "0.0", "HasPunishment": False}
    result = parse_lookup_result(
        _payload(document=DNI, debt=debt), expected_document=DNI
    )
    assert result.subject == DNI
    assert result.columns["debt_total"] == "0.00"


def test_zero_debt_is_a_success_when_has_error_is_false() -> None:
    debt = {
        "DocumentNumber": RUC,
        "DebtTotal": "0.0",
        "HasPunishment": False,
        "Accounts": {"List": []},
    }
    result = parse_lookup_result(_payload(debt=debt), expected_document=RUC)
    assert result.columns["debt_total"] == "0.00"


def test_rejected_shape_is_not_misread_as_zero_debt() -> None:
    with pytest.raises(RejectedError):
        parse_lookup_result(_payload(hasError=True), expected_document=RUC)


def test_response_for_another_document_is_rejected() -> None:
    with pytest.raises(BrowserError, match="another document"):
        parse_lookup_result(_payload(document="20100000001"), expected_document=RUC)


@pytest.mark.parametrize("total", ["not-money", "-1", "NaN"])
def test_invalid_debt_total_is_rejected(total: str) -> None:
    debt = {
        "DocumentNumber": RUC,
        "DebtTotal": total,
        "HasPunishment": False,
    }
    with pytest.raises(BrowserError, match="total is invalid"):
        parse_lookup_result(_payload(debt=debt), expected_document=RUC)
