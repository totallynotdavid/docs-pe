from __future__ import annotations

import pytest

from capture.ruc import RUC


def test_accepts_an_eleven_digit_ruc_and_strips_whitespace() -> None:
    assert str(RUC(" 20100000092 ")) == "20100000092"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "2010000009",
        "201000000921",
        "2010000009a",
        "٢٠١٠٠٠٠٠٠٩٢",
    ],
)
def test_rejects_anything_that_is_not_an_eleven_digit_ruc(value: str) -> None:
    with pytest.raises(ValueError, match="invalid RUC"):
        RUC(value)
