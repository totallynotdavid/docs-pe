from __future__ import annotations

import csv

from typing import TYPE_CHECKING

from capture.sites.entel.site import ENTEL
from capture.store import ObservationStore


if TYPE_CHECKING:
    from pathlib import Path


def _columns(total: str) -> dict[str, str]:
    return {"debt_total": total, "has_punishment": "False"}


def test_exports_the_latest_verified_debt(tmp_path: Path) -> None:
    output = tmp_path / "current.csv"
    with ObservationStore(tmp_path / "state.sqlite3") as store:
        store.record_success(
            run_id="one", site="entel", ruc="20100000092", columns=_columns("700.00")
        )
        store.record_failure(
            run_id="two",
            site="entel",
            ruc="20100000092",
            status="rejected",
            error_detail="ambiguous",
        )
        store.record_success(
            run_id="three",
            site="entel",
            ruc="20100000092",
            columns=_columns("726.91"),
        )
        store.export_current(
            output, site="entel", header=ENTEL.export_header, project=ENTEL.row
        )
        assert store.observation_count() == 3
        assert store.latest("entel", "20100000092") == _columns("726.91")

    with output.open(newline="", encoding="utf-8") as file_obj:
        rows = list(csv.DictReader(file_obj))
    assert rows[0]["debt_total"] == "726.91"
