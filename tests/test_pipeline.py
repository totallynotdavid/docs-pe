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


def _failure(ruc: str, *, code: str, attempt: int) -> LookupResult:
    return LookupResult(
        ruc=RUC(ruc),
        status=Status.FAILED,
        error_code=code,
        error_detail=code,
        attempt=attempt,
    )


def test_below_cap_failure_is_retryable_not_done(tmp_path: Path) -> None:
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
        # Below the cap: stays eligible for re-fetch, so it is not done.
        log.record_failure(_failure(RUC_B, code="ban_signal", attempt=4))

        counts = log.counts()
        assert counts == OutcomeCounts(succeeded=1, terminal_failed=0, retryable=1)
        assert log.done_rucs() == {RUC_A}

        export_csv(log=log, output_csv=output_csv)

    success = _read_csv(output_csv)
    assert success[0] == ["ruc", "carrier", "lines", "total_lines"]
    assert [RUC_A, "claro", "3", "5"] in success

    errors = _read_csv(output_csv.with_suffix(".errors.csv"))
    assert errors[0][1] == "error_code"
    assert errors[0][2] == "error_detail"
    ban_row = next(row for row in errors[1:] if row[0] == RUC_B)
    assert ban_row[1] == "ban_signal"
    assert ban_row[3] == "4"


def test_healthy_attempts_accumulate_until_cap(tmp_path: Path) -> None:
    with OutcomeLog(state_path_for_output(tmp_path / "out.csv")) as log:
        # The same RUC failing across several healthy runs accumulates attempts;
        # below the cap it stays retryable, then retires as terminal at the cap.
        log.record_failure(_failure(RUC_A, code="transport_error", attempt=4))
        assert log.done_rucs() == set()
        assert log.counts().retryable == 1

        while log.counts().retryable == 1:
            log.record_failure(_failure(RUC_A, code="transport_error", attempt=4))

        assert log.counts() == OutcomeCounts(
            succeeded=0, terminal_failed=1, retryable=0
        )
        assert log.done_rucs() == {RUC_A}


def test_outage_attempts_do_not_advance_cap(tmp_path: Path) -> None:
    with OutcomeLog(state_path_for_output(tmp_path / "out.csv")) as log:
        # An attempt made while the provider was unhealthy must not count toward
        # the cap, so no length of outage can retire a valid RUC.
        for _ in range(10):
            log.record_failure(
                LookupResult(
                    ruc=RUC(RUC_A),
                    status=Status.FAILED,
                    error_code="transport_error",
                    error_detail="outage",
                    made_healthy_contact=False,
                    attempt=4,
                )
            )
        assert log.counts() == OutcomeCounts(
            succeeded=0, terminal_failed=0, retryable=1
        )
        assert log.done_rucs() == set()


def test_success_supersedes_prior_failure(tmp_path: Path) -> None:
    with OutcomeLog(state_path_for_output(tmp_path / "out.csv")) as log:
        log.record_failure(_failure(RUC_A, code="ban_signal", attempt=4))
        log.record_success(
            LookupResult(ruc=RUC(RUC_A), status=Status.OK, total_lines=1, attempt=2)
        )
        assert log.counts() == OutcomeCounts(
            succeeded=1, terminal_failed=0, retryable=0
        )
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
