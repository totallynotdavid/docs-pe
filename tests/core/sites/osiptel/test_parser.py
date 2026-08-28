from __future__ import annotations

import pytest

from core.domain.errors import ProviderSchemaError
from core.sites.osiptel.parser import parse_page


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "modalidad": "POSTPAGO",
        "numeroServicio": "98857****",
        "operador": "AMERICA MOVIL PERU S.A.C.",
    }
    row.update(overrides)
    return row


def _page(**overrides: object) -> dict[str, object]:
    page: dict[str, object] = {"iTotalRecords": 1, "data": [_row()]}
    page.update(overrides)
    return page


def test_extracts_one_row_per_line() -> None:
    parsed = parse_page(
        _page(
            data=[
                _row(modalidad="PREPAGO", numeroServicio="96222****", operador="CLARO"),
                _row(
                    modalidad="POSTPAGO", numeroServicio="94915****", operador="ENTEL"
                ),
            ],
            iTotalRecords=2,
        )
    )
    assert parsed.rows == (
        ("PREPAGO", "96222****", "CLARO"),
        ("POSTPAGO", "94915****", "ENTEL"),
    )
    assert parsed.total_records == 2


def test_accepts_total_records_as_a_numeric_string() -> None:
    # The DataTables endpoint sometimes serializes the count as a string.
    assert parse_page(_page(iTotalRecords="5")).total_records == 5


def test_an_empty_data_array_is_a_valid_empty_page() -> None:
    parsed = parse_page(_page(data=[], iTotalRecords=0))
    assert parsed.rows == ()


def test_the_rejection_flag_raises() -> None:
    with pytest.raises(ProviderSchemaError, match="rejected"):
        parse_page(_page(estado=True))


@pytest.mark.parametrize(
    "payload",
    [
        "not-a-dict",
        {"data": [_row()]},
        {"iTotalRecords": 1},
        {"iTotalRecords": 1, "data": ["not-a-dict"]},
        {"iTotalRecords": 1, "data": [{"modalidad": "POSTPAGO", "operador": "CLARO"}]},
        {"iTotalRecords": 1, "data": [_row(operador="  ")]},
        {"iTotalRecords": 1, "data": [{}]},
    ],
)
def test_a_malformed_payload_raises(payload: object) -> None:
    with pytest.raises(ProviderSchemaError):
        parse_page(payload)
