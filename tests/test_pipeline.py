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

        assert store.summary().pending == 2

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

        summary = store.summary()
        assert summary.pending == 0
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


def test_claim_next_is_fifo_and_exhausts(tmp_path: Path) -> None:
    # The store is the queue: claim hands out each pending RUC exactly once in id
    # order, then signals "drained" with None. This is the only coordination
    # between lanes and processes, so it has to be exact.
    store_path = state_path_for_output(tmp_path / "out.csv")
    with JobStore(store_path) as store:
        store.insert_pending(RUC(RUC_A))
        store.insert_pending(RUC(RUC_B))

        first = store.claim_next(owner="lane-1", lease_s=600.0)
        second = store.claim_next(owner="lane-2", lease_s=600.0)
        assert [first, second] == [RUC(RUC_A), RUC(RUC_B)]
        assert store.claim_next(owner="lane-3", lease_s=600.0) is None

        summary = store.summary()
        assert summary.pending == 0
        assert summary.in_progress == 2


def test_reset_expired_leases_requeues_only_expired(tmp_path: Path) -> None:
    # A crashed lane's job (expired lease) must return to pending; a live lane's
    # job (unexpired lease) must not be stolen out from under it.
    store_path = state_path_for_output(tmp_path / "out.csv")
    with JobStore(store_path) as store:
        store.insert_pending(RUC(RUC_A))
        store.insert_pending(RUC(RUC_B))

        live = store.claim_next(owner="alive", lease_s=600.0)
        dead = store.claim_next(owner="crashed", lease_s=0.0)
        assert [live, dead] == [RUC(RUC_A), RUC(RUC_B)]

        assert store.reset_expired_leases() == 1
        assert store.claim_next(owner="recovery", lease_s=600.0) == RUC(RUC_B)
        assert store.claim_next(owner="recovery", lease_s=600.0) is None
