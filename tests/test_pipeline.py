from __future__ import annotations

import csv

from typing import TYPE_CHECKING

from robot.domain.types import RUC, CarrierCount, LookupResult, Status
from robot.jobs.exporter import export_csv
from robot.jobs.store import JobStore, state_path_for_output


if TYPE_CHECKING:
    from pathlib import Path


RUC_A = "20100000001"
RUC_B = "20100000002"


def _read_csv(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as file_obj:
        return list(csv.reader(file_obj))


def test_pipeline_exports_success_and_error_rows(tmp_path: Path) -> None:
    # End to end across the binding boundary: a transposed parameter tuple would
    # surface here as wrong cells in the exported CSV, which no static check sees.
    output_csv = tmp_path / "out.csv"
    store_path = state_path_for_output(output_csv)

    with JobStore(store_path) as store:
        store.insert_pending(RUC(RUC_A))
        store.insert_pending(RUC(RUC_B))

        assert store.pending_rucs() == [RUC(RUC_A), RUC(RUC_B)]

        store.complete_success(
            ruc=RUC(RUC_A),
            result=LookupResult(
                ruc=RUC(RUC_A),
                status=Status.OK,
                total_lines=5,
                carrier_counts=(
                    CarrierCount(carrier="claro", lines=3),
                    CarrierCount(carrier="movistar", lines=2),
                ),
                http_session_id="sess-1",
                proxy_id="proxy-1",
                attempt=1,
            ),
        )
        store.complete_failure(
            ruc=RUC(RUC_B),
            result=LookupResult(
                ruc=RUC(RUC_B),
                status=Status.FAILED,
                error_code="ban_signal",
                error_detail="blocked",
                http_session_id="sess-2",
                proxy_id="proxy-2",
                attempt=3,
            ),
        )

        # A completed RUC is no longer pending; only the failed one would re-run.
        assert store.pending_rucs() == []
        summary = store.summary()
        assert summary.succeeded == 1
        assert summary.failed == 1

        export_csv(store=store, output_csv=output_csv)

    success = _read_csv(output_csv)
    assert success[0] == ["ruc", "carrier", "lines", "total_lines"]
    assert [RUC_A, "claro", "3", "5"] in success
    assert [RUC_A, "movistar", "2", "5"] in success

    errors = _read_csv(output_csv.with_suffix(".errors.csv"))
    assert errors[0] == [
        "ruc",
        "error_code",
        "error_detail",
        "attempt",
        "session_id",
        "proxy_id",
        "timestamp",
    ]
    error_row = next(row for row in errors[1:] if row[0] == RUC_B)
    assert error_row[1] == "ban_signal"
    assert error_row[2] == "blocked"
    assert error_row[3] == "3"
    assert error_row[4] == "sess-2"
    assert error_row[5] == "proxy-2"
