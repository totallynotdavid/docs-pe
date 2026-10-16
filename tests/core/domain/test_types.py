from __future__ import annotations

import pytest

from core.domain.types import Doc, DocKind, RucKind


@pytest.mark.parametrize(
    ("value", "canonical"),
    [
        ("20100000001", "20100000001"),
        ("42953322", "42953322"),
        ("  20100000001  ", "20100000001"),
        ("2953322", "02953322"),
    ],
)
def test_a_document_is_stored_in_its_canonical_form(
    value: str,
    canonical: str,
) -> None:
    assert str(Doc(value)) == canonical


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
        ("2953322", DocKind.DNI),
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
