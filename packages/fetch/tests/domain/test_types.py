from __future__ import annotations

import pytest

from fetch.domain.types import RUC, RucKind


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


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("20100000001", RucKind.JURIDICA),
        ("10100000001", RucKind.NATURAL),
    ],
)
def test_kind_is_derived_from_the_leading_digits(value: str, kind: RucKind) -> None:
    # Pin the routing key directly; the planner reads it via _serves in plan.py.
    assert RUC(value).kind == kind
