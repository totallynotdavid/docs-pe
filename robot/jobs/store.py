from __future__ import annotations

import csv
import sqlite3

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from robot.domain.types import RUC, CarrierCount, LookupResult, Status
from robot.jobs.exporter import SUCCESS_HEADERS


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


JOB_PENDING = "pending"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"


# The store is the durable source of truth, owned by one process per run. A job
# is pending, succeeded, or failed; there is no "running" state. A crash leaves
# in-flight jobs on pending, so a restart re-runs exactly them with no orphan
# recovery or lease bookkeeping.
SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ruc TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_detail TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    proxy_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_id ON jobs(status, id);

CREATE TABLE IF NOT EXISTS results (
    ruc TEXT NOT NULL,
    carrier TEXT NOT NULL,
    lines INTEGER NOT NULL,
    total_lines INTEGER NOT NULL,
    PRIMARY KEY (ruc, carrier),
    FOREIGN KEY (ruc) REFERENCES jobs(ruc) ON DELETE CASCADE
);
"""

INSERT_JOB = """
INSERT OR IGNORE INTO jobs (ruc, status, created_at, updated_at)
VALUES (:ruc, :status, :created_at, :updated_at)
"""

REQUEUE_FAILED_JOB = """
UPDATE jobs
   SET status = :status,
       last_error_code = '',
       last_error_detail = '',
       updated_at = :updated_at,
       finished_at = NULL
 WHERE ruc = :ruc AND status = :from_status
"""

SELECT_PENDING_RUCS = "SELECT ruc FROM jobs WHERE status = :pending ORDER BY id"

DELETE_RESULTS_FOR_RUC = "DELETE FROM results WHERE ruc = :ruc"

INSERT_RESULT = """
INSERT INTO results (ruc, carrier, lines, total_lines)
VALUES (:ruc, :carrier, :lines, :total_lines)
"""

MARK_JOB_SUCCEEDED = """
UPDATE jobs
   SET status = :status,
       attempt_count = :attempt_count,
       last_error_code = '',
       last_error_detail = '',
       session_id = :session_id,
       proxy_id = :proxy_id,
       finished_at = :finished_at,
       updated_at = :updated_at
 WHERE ruc = :ruc
"""

MARK_JOB_FAILED = """
UPDATE jobs
   SET status = :status,
       attempt_count = :attempt_count,
       last_error_code = :last_error_code,
       last_error_detail = :last_error_detail,
       session_id = :session_id,
       proxy_id = :proxy_id,
       finished_at = :finished_at,
       updated_at = :updated_at
 WHERE ruc = :ruc
"""

SELECT_STATUS_COUNTS = "SELECT status, COUNT(*) AS total FROM jobs GROUP BY status"

SELECT_RESULT_ROWS = """
SELECT ruc, carrier, lines, total_lines
  FROM results
 ORDER BY ruc, carrier
"""

SELECT_ERROR_ROWS = """
SELECT ruc,
       last_error_code,
       last_error_detail,
       attempt_count,
       session_id,
       proxy_id,
       finished_at
  FROM jobs
 WHERE status = :status
 ORDER BY ruc
"""


@dataclass(frozen=True)
class StoreSummary:
    pending: int
    succeeded: int
    failed: int


class JobStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> JobStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def insert_pending(self, ruc: RUC) -> bool:
        now = _now()
        cursor = self._conn.execute(
            INSERT_JOB,
            {
                "ruc": str(ruc),
                "status": JOB_PENDING,
                "created_at": now,
                "updated_at": now,
            },
        )
        if cursor.rowcount > 0:
            return True

        cursor = self._conn.execute(
            REQUEUE_FAILED_JOB,
            {
                "status": JOB_PENDING,
                "updated_at": now,
                "ruc": str(ruc),
                "from_status": JOB_FAILED,
            },
        )
        return cursor.rowcount > 0

    def pending_rucs(self) -> list[RUC]:
        rows = self._conn.execute(
            SELECT_PENDING_RUCS, {"pending": JOB_PENDING}
        ).fetchall()
        return [RUC(str(row["ruc"])) for row in rows]

    def complete_success(self, *, ruc: RUC, result: LookupResult) -> None:
        now = _now()
        with self._transaction():
            self._conn.execute(DELETE_RESULTS_FOR_RUC, {"ruc": str(ruc)})
            carriers = result.carrier_counts or (
                CarrierCount(carrier="unknown", lines=result.total_lines),
            )
            self._conn.executemany(
                INSERT_RESULT,
                [
                    {
                        "ruc": str(ruc),
                        "carrier": item.carrier,
                        "lines": item.lines,
                        "total_lines": result.total_lines,
                    }
                    for item in carriers
                ],
            )
            self._conn.execute(
                MARK_JOB_SUCCEEDED,
                {
                    "status": JOB_SUCCEEDED,
                    "attempt_count": result.attempt,
                    "session_id": result.http_session_id,
                    "proxy_id": result.proxy_id,
                    "finished_at": now,
                    "updated_at": now,
                    "ruc": str(ruc),
                },
            )

    def complete_failure(self, *, ruc: RUC, result: LookupResult) -> None:
        now = _now()
        with self._transaction():
            self._conn.execute(
                MARK_JOB_FAILED,
                {
                    "status": JOB_FAILED,
                    "attempt_count": result.attempt,
                    "last_error_code": result.error_code,
                    "last_error_detail": result.error_detail,
                    "session_id": result.http_session_id,
                    "proxy_id": result.proxy_id,
                    "finished_at": now,
                    "updated_at": now,
                    "ruc": str(ruc),
                },
            )

    def summary(self) -> StoreSummary:
        rows = self._conn.execute(SELECT_STATUS_COUNTS).fetchall()
        totals = {str(row["status"]): int(row["total"]) for row in rows}
        return StoreSummary(
            pending=totals.get(JOB_PENDING, 0),
            succeeded=totals.get(JOB_SUCCEEDED, 0),
            failed=totals.get(JOB_FAILED, 0),
        )

    def result_rows(self) -> Iterator[list[str | int]]:
        rows = self._conn.execute(SELECT_RESULT_ROWS)
        for row in rows:
            yield [
                str(row["ruc"]),
                str(row["carrier"]),
                int(row["lines"]),
                int(row["total_lines"]),
            ]

    def error_rows(self) -> Iterator[list[str]]:
        rows = self._conn.execute(SELECT_ERROR_ROWS, {"status": JOB_FAILED})
        for row in rows:
            yield [
                str(row["ruc"]),
                str(row["last_error_code"]),
                str(row["last_error_detail"]),
                str(row["attempt_count"]),
                str(row["session_id"]),
                str(row["proxy_id"]),
                str(row["finished_at"]),
            ]

    def seed_success_csv(self, path: Path) -> int:
        if not path.exists() or path.stat().st_size == 0:
            return 0

        grouped: dict[str, list[CarrierCount]] = {}
        totals: dict[str, int] = {}
        with path.open(newline="", encoding="utf-8") as file_obj:
            reader = csv.reader(file_obj)
            header = next(reader, [])
            if header != SUCCESS_HEADERS:
                return 0
            for row in reader:
                if len(row) != len(SUCCESS_HEADERS):
                    continue
                ruc_raw, carrier, lines_raw, total_raw = row
                try:
                    ruc = RUC(ruc_raw)
                    lines = int(lines_raw)
                    total = int(total_raw)
                except (TypeError, ValueError):
                    continue
                grouped.setdefault(str(ruc), []).append(
                    CarrierCount(carrier=carrier, lines=lines)
                )
                totals[str(ruc)] = total

        seeded = 0
        for ruc_raw, carriers in grouped.items():
            ruc = RUC(ruc_raw)
            self.insert_pending(ruc)
            result = LookupResult(
                ruc=ruc,
                status=Status.OK,
                total_lines=totals[ruc_raw],
                carrier_counts=tuple(carriers),
            )
            self.complete_success(ruc=ruc, result=result)
            seeded += 1
        return seeded

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def _configure(self) -> None:
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        self._conn.execute("PRAGMA foreign_keys = ON")

    def _migrate(self) -> None:
        self._conn.executescript(SCHEMA_DDL)


def state_path_for_output(output_csv: Path) -> Path:
    return output_csv.with_suffix(".state.sqlite3")


def _now() -> str:
    return datetime.now(UTC).isoformat()
