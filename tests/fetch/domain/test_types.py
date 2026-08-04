from __future__ import annotations

import pytest

from fetch.domain.types import Doc, DocKind, RucKind


def test_accepts_eleven_digit_ruc() -> None:
    assert str(Doc("20100000001")) == "20100000001"


def test_accepts_eight_digit_dni() -> None:
    assert str(Doc("42953322")) == "42953322"


def test_strips_surrounding_whitespace() -> None:
    assert str(Doc("  20100000001  ")) == "20100000001"


def test_pads_a_seven_digit_dni_to_the_canonical_width() -> None:
    # Old 7-digit DNIs are the modern 8-digit form with a dropped leading zero.
    doc = Doc("2953322")

    assert str(doc) == "02953322"
    assert doc.kind is DocKind.DNI


@pytest.mark.parametrize(
    "value",
    [
        "123456",
        "123456789",
        "2010000000",
        "201000000012",
        "2010000000a",
        "",
        "abcdefgh",
    ],
)
def test_rejects_anything_that_is_not_a_dni_or_ruc(value: str) -> None:
    with pytest.raises(ValueError, match="invalid document"):
        Doc(value)


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("20100000001", DocKind.RUC),
        ("10100000001", DocKind.RUC),
        ("42953322", DocKind.DNI),
    ],
)
def test_kind_is_derived_from_the_shape(value: str, kind: DocKind) -> None:
    assert Doc(value).kind is kind


@pytest.mark.parametrize(
    ("value", "ruc_kind"),
    [
        ("20100000001", RucKind.JURIDICA),
        ("10100000001", RucKind.NATURAL),
    ],
)
def test_ruc_kind_is_derived_from_the_leading_digits(
    value: str,
    ruc_kind: RucKind,
) -> None:
    assert Doc(value).ruc_kind is ruc_kind


def test_a_dni_has_no_ruc_kind() -> None:
    assert Doc("42953322").ruc_kind is None
