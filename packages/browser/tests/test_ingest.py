from __future__ import annotations

from typing import TYPE_CHECKING

from browser.ingest import read_subjects


if TYPE_CHECKING:
    from pathlib import Path


def test_read_subjects_dedupes_and_counts(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("20131312955\n\nnot-a-ruc\n20131312955\n987654321\n")
    subjects, counts = read_subjects(csv_path, dedupe=True)
    assert [str(s) for s in subjects] == ["20131312955", "987654321"]
    assert (counts.rows_read, counts.valid, counts.ignored, counts.duplicates) == (
        5,
        2,
        2,
        1,
    )
