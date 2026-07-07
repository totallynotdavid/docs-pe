from __future__ import annotations

import pytest

from robot.domain.errors import ProviderSchemaError
from robot.store.payload import decode_rows, encode_rows


def test_round_trips_mixed_string_and_int_cells() -> None:
    rows = (("CLARO", 2, 100), ("ENTEL", 1, 100))
    assert decode_rows(encode_rows(rows)) == rows


def test_empty_rows_round_trip() -> None:
    assert encode_rows(()) == "[]"
    assert decode_rows("[]") == ()


@pytest.mark.parametrize("text", ['{"not": "a list"}', "42", '"string"'])
def test_a_top_level_non_list_payload_is_rejected(text: str) -> None:
    with pytest.raises(ProviderSchemaError):
        decode_rows(text)


@pytest.mark.parametrize("text", ['["not-a-row"]', "[[1.5]]", "[[null]]", '[{"k": 1}]'])
def test_a_malformed_row_is_rejected(text: str) -> None:
    with pytest.raises(ProviderSchemaError):
        decode_rows(text)
