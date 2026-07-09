from __future__ import annotations

import pytest

from robot.domain.errors import ProviderSchemaError
from robot.sites.osiptel.parser import parse_page


def _page(**overrides: object) -> dict[str, object]:
    page: dict[str, object] = {
        "iTotalRecords": 2,
        "data": [{"operador": "CLARO"}, {"operador": "MOVISTAR"}],
    }
    page.update(overrides)
    return page


def test_counts_lines_per_carrier() -> None:
    parsed = parse_page(
        _page(data=[{"operador": "CLARO"}, {"operador": "CLARO"}], iTotalRecords=2)
    )
    assert parsed.carrier_counts == {"CLARO": 2}
    assert parsed.total_records == 2
    assert parsed.rows_returned == 2


def test_accepts_total_records_as_a_numeric_string() -> None:
    # The DataTables endpoint sometimes serializes the count as a string.
    assert parse_page(_page(iTotalRecords="5")).total_records == 5


def test_an_empty_data_array_is_a_valid_empty_page() -> None:
    parsed = parse_page(_page(data=[], iTotalRecords=0))
    assert parsed.carrier_counts == {}
    assert parsed.rows_returned == 0


def test_the_rejection_flag_raises() -> None:
    with pytest.raises(ProviderSchemaError, match="rejected"):
        parse_page(_page(estado=True))


@pytest.mark.parametrize(
    "payload",
    [
        "not-a-dict",
        {"data": [{"operador": "CLARO"}]},
        {"iTotalRecords": 1},
        {"iTotalRecords": 1, "data": ["not-a-dict"]},
        {"iTotalRecords": 1, "data": [{"operador": "  "}]},
        {"iTotalRecords": 1, "data": [{}]},
    ],
)
def test_a_malformed_payload_raises(payload: object) -> None:
    with pytest.raises(ProviderSchemaError):
        parse_page(payload)
