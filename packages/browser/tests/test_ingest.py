from __future__ import annotations

import pytest

from browser.ingest import read_rucs
from browser.ruc import RUC


def test_ruc_normalizes_and_validates() -> None:
    assert str(RUC("  20131312955 ")) == "20131312955"
    with pytest.raises(ValueError, match="11 digits"):
        RUC("123")


def test_read_rucs_dedupes_and_counts(tmp_path) -> None:
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("20131312955\n\nnot-a-ruc\n20131312955\n10412345678\n")
    rucs, counts = read_rucs(csv_path, dedupe=True)
    assert [str(r) for r in rucs] == ["20131312955", "10412345678"]
    assert (counts.rows_read, counts.valid, counts.ignored, counts.duplicates) == (
        5,
        2,
        2,
        1,
    )
