from __future__ import annotations

import pytest

from robot.domain.types import RUC


def test_accepts_eleven_digits() -> None:
    assert str(RUC("20100000001")) == "20100000001"


def test_strips_surrounding_whitespace() -> None:
    assert str(RUC("  20100000001  ")) == "20100000001"


@pytest.mark.parametrize(
    "value",
    ["2010000000", "201000000012", "2010000000a", "", "abcdefghijk"],
)
def test_rejects_anything_that_is_not_eleven_digits(value: str) -> None:
    with pytest.raises(ValueError, match="invalid RUC"):
        RUC(value)
