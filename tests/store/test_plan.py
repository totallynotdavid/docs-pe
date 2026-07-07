from __future__ import annotations

from typing import TYPE_CHECKING

from robot.domain.types import RUC
from robot.store.plan import plan_pending, read_rucs


if TYPE_CHECKING:
    from pathlib import Path


def test_reads_valid_rucs_and_dedupes(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("20100000001\n20100000002\n20100000001\n", encoding="utf-8")
    rucs, counts = read_rucs(csv_path, dedupe=True)
    assert [str(r) for r in rucs] == ["20100000001", "20100000002"]
    assert counts.valid == 2
    assert counts.duplicates == 1
    assert counts.rows_read == 3


def test_keeps_duplicates_when_dedupe_is_off(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("20100000001\n20100000001\n", encoding="utf-8")
    rucs, counts = read_rucs(csv_path, dedupe=False)
    assert len(rucs) == 2
    assert counts.duplicates == 0


def test_blank_and_invalid_rows_are_ignored(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("20100000001\n\nnot-a-ruc\n123\n", encoding="utf-8")
    rucs, counts = read_rucs(csv_path, dedupe=True)
    assert [str(r) for r in rucs] == ["20100000001"]
    assert counts.ignored == 3
    assert counts.valid == 1


def test_strips_a_utf8_bom(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    # A CSV exported from a spreadsheet often carries a BOM on the first cell; it
    # must not turn a valid RUC into an invalid one.
    csv_path.write_text("20100000001\n", encoding="utf-8-sig")
    rucs, counts = read_rucs(csv_path, dedupe=True)
    assert [str(r) for r in rucs] == ["20100000001"]
    assert counts.valid == 1


def test_plan_pending_excludes_already_done_pairs() -> None:
    rucs = [RUC("20100000001"), RUC("20100000002")]
    done = {("osiptel", "20100000001")}
    pending = plan_pending(rucs, ["osiptel", "sunat"], done)
    assert [str(r) for r in pending["osiptel"]] == ["20100000002"]
    assert [str(r) for r in pending["sunat"]] == ["20100000001", "20100000002"]
