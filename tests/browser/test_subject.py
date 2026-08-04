from __future__ import annotations

import pytest

from browser.subject import Subject, SubjectKind


def test_classifies_by_digit_shape() -> None:
    assert Subject("987654321").kind is SubjectKind.PHONE
    assert Subject("12345678").kind is SubjectKind.DNI
    assert Subject("20131312955").kind is SubjectKind.RUC


def test_pads_seven_digit_dni_to_canonical_width() -> None:
    subject = Subject("7654321")
    assert subject.kind is SubjectKind.DNI
    assert str(subject) == "07654321"


def test_strips_surrounding_whitespace() -> None:
    assert str(Subject("  20131312955 ")) == "20131312955"


@pytest.mark.parametrize(
    "value",
    ["123", "abc", "812345678", "1234567890123"],
)
def test_rejects_unclassifiable(value: str) -> None:
    with pytest.raises(ValueError, match="invalid subject"):
        Subject(value)
