from __future__ import annotations

import csv

from typing import TYPE_CHECKING

from robot.domain.types import RUC, CarrierCount, LookupResult, Status
from robot.store.export import export_csv
from robot.store.outcome_log import OutcomeCounts, OutcomeLog, state_path_for_output
from robot.store.plan import derive_pending


if TYPE_CHECKING:
    from pathlib import Path


RUC_A = "20100000001"
RUC_B = "20100000002"
RUC_C = "20100000003"


def _read_csv(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as file_obj:
        return list(csv.reader(file_obj))


def _write_input(path: Path, rucs: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as file_obj:
        writer = csv.writer(file_obj)
        for ruc in rucs:
            writer.writerow([ruc])


def test_log_exports_terminal_rows_and_done_set(tmp_path: Path) -> None:
    output_csv = tmp_path / "out.csv"
    with OutcomeLog(state_path_for_output(output_csv)) as log:
        log.record_success(
            LookupResult(
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
            )
        )
        log.record_failure(
            LookupResult(
                ruc=RUC(RUC_B),
                status=Status.FAILED,
                error_code="ban_signal",
                error_detail="blocked",
                http_session_id="sess-2",
                proxy_id="proxy-2",
                attempt=4,
            )
        )

        counts = log.counts()
        assert counts.succeeded == 1
        assert counts.failed == 1
        assert log.done_rucs() == {RUC_A, RUC_B}

        export_csv(log=log, output_csv=output_csv)

    success = _read_csv(output_csv)
    assert success[0] == ["ruc", "carrier", "lines", "total_lines"]
    assert [RUC_A, "claro", "3", "5"] in success
    assert [RUC_A, "movistar", "2", "5"] in success

    errors = _read_csv(output_csv.with_suffix(".errors.csv"))
    error_row = next(row for row in errors[1:] if row[0] == RUC_B)
    assert error_row[1] == "ban_signal"
    assert error_row[3] == "4"


def test_success_supersedes_prior_failure(tmp_path: Path) -> None:
    with OutcomeLog(state_path_for_output(tmp_path / "out.csv")) as log:
        log.record_failure(
            LookupResult(ruc=RUC(RUC_A), status=Status.FAILED, attempt=4)
        )
        log.record_success(
            LookupResult(ruc=RUC(RUC_A), status=Status.OK, total_lines=1, attempt=2)
        )
        assert log.counts() == OutcomeCounts(succeeded=1, failed=0)
        assert log.done_rucs() == {RUC_A}


def test_derive_pending_excludes_done(tmp_path: Path) -> None:
    input_csv = tmp_path / "in.csv"
    _write_input(input_csv, [RUC_A, RUC_B, RUC_C, RUC_B])

    pending, plan = derive_pending(input_csv=input_csv, done={RUC_A}, dedupe=True)
    assert [str(ruc) for ruc in pending] == [RUC_B, RUC_C]
    assert plan.valid == 3
    assert plan.duplicates == 1
    assert plan.already_done == 1
    assert plan.pending == 2


def test_import_csv_reseeds_done_from_success_export(tmp_path: Path) -> None:
    output_csv = tmp_path / "out.csv"
    with OutcomeLog(state_path_for_output(output_csv)) as log:
        log.record_success(
            LookupResult(
                ruc=RUC(RUC_A),
                status=Status.OK,
                total_lines=3,
                carrier_counts=(CarrierCount(carrier="claro", lines=3),),
                attempt=1,
            )
        )
        export_csv(log=log, output_csv=output_csv)

    state_path_for_output(output_csv).unlink()
    with OutcomeLog(state_path_for_output(output_csv)) as fresh:
        assert fresh.done_rucs() == set()
        assert fresh.import_csv(output_csv) == 1
        assert fresh.done_rucs() == {RUC_A}
