from __future__ import annotations

import csv

from datetime import UTC, datetime
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

        first = store.claim_next(worker_id=1)
        second = store.claim_next(worker_id=1)
        assert first is not None
        assert second is not None

        store.complete_success(
            job=first,
            result=LookupResult(
                ruc=first.ruc,
                status=Status.OK,
                total_lines=5,
                carrier_counts=(
                    CarrierCount(carrier="claro", lines=3),
                    CarrierCount(carrier="movistar", lines=2),
                ),
                session_id="sess-1",
                proxy_id="proxy-1",
            ),
        )
        store.complete_failure(
            job=second,
            result=LookupResult(
                ruc=second.ruc,
                status=Status.FAILED,
                error_code="ban",
                error_detail="blocked",
                session_id="sess-2",
                proxy_id="proxy-2",
            ),
        )

        export_csv(store=store, output_csv=output_csv)

    success = _read_csv(output_csv)
    assert success[0] == ["ruc", "carrier", "lines", "total_lines"]
    assert [str(first.ruc), "claro", "3", "5"] in success
    assert [str(first.ruc), "movistar", "2", "5"] in success

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
    error_row = next(row for row in errors[1:] if row[0] == str(second.ruc))
    assert error_row[1] == "ban"
    assert error_row[2] == "blocked"
    assert error_row[4] == "sess-2"
    assert error_row[5] == "proxy-2"


def test_expired_lease_is_reclaimable(tmp_path: Path) -> None:
    output_csv = tmp_path / "out.csv"
    store_path = state_path_for_output(output_csv)

    with JobStore(store_path) as store:
        store.insert_pending(RUC(RUC_A))

        claimed = store.claim_next(worker_id=1)
        assert claimed is not None
        assert claimed.attempt_no == 1

        # The claim must actually stamp a lease in the future, not merely flip
        # the job to running; otherwise expiry-based reclaim never fires.
        lease = store._conn.execute(
            "SELECT claimed_until FROM jobs WHERE id = :id",
            {"id": claimed.id},
        ).fetchone()["claimed_until"]
        assert lease is not None
        assert lease > datetime.now(UTC).isoformat()

        # A live lease blocks any other worker from grabbing the same job.
        assert store.claim_next(worker_id=2) is None

        # Backdate the lease to stand in for a worker that died mid-flight.
        store._conn.execute(
            "UPDATE jobs SET claimed_until = :past WHERE id = :id",
            {"past": "2000-01-01T00:00:00+00:00", "id": claimed.id},
        )

        reclaimed = store.claim_next(worker_id=2)
        assert reclaimed is not None
        assert reclaimed.id == claimed.id
        assert reclaimed.ruc == claimed.ruc
        assert reclaimed.attempt_no == 2
