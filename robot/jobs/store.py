from __future__ import annotations

import csv
import sqlite3

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from robot.domain.types import RUC, CarrierCount, LookupResult, Status
from robot.jobs.exporter import SUCCESS_HEADERS


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


JOB_PENDING = "pending"
JOB_IN_PROGRESS = "in_progress"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"


# The store is the queue. Jobs move from pending to in_progress under a lease,
# then to a terminal status. Expired leases are the recovery boundary.
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
    lease_owner TEXT NOT NULL DEFAULT '',
    lease_expires_at TEXT,
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

CLAIM_SELECT_PENDING = (
    "SELECT id, ruc FROM jobs WHERE status = :pending ORDER BY id LIMIT 1"
)

CLAIM_MARK_IN_PROGRESS = """
UPDATE jobs
   SET status = :in_progress,
       lease_owner = :owner,
       lease_expires_at = :lease_expires_at,
       updated_at = :updated_at
 WHERE id = :id AND status = :pending
"""

RESET_EXPIRED_LEASES = """
UPDATE jobs
   SET status = :pending,
       lease_owner = '',
       lease_expires_at = NULL,
       updated_at = :updated_at
 WHERE status = :in_progress
   AND (lease_expires_at IS NULL OR lease_expires_at <= :now)
"""

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
       lease_owner = '',
       lease_expires_at = NULL,
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
       lease_owner = '',
       lease_expires_at = NULL,
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
    in_progress: int
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
        self._create_schema()

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

    def claim_next(self, *, owner: str, lease_s: float) -> RUC | None:
        """Atomically take the oldest pending job, leasing it to `owner`.

        The SELECT and UPDATE run inside one BEGIN IMMEDIATE transaction, so the
        write lock serializes claims across every lane and process sharing this
        DB: no two callers ever get the same RUC. Returns None when nothing is
        pending, which the caller reads as "queue drained, stop".
        """
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        lease_expires_at = (now_dt + timedelta(seconds=lease_s)).isoformat()
        with self._transaction():
            row = self._conn.execute(
                CLAIM_SELECT_PENDING, {"pending": JOB_PENDING}
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                CLAIM_MARK_IN_PROGRESS,
                {
                    "in_progress": JOB_IN_PROGRESS,
                    "owner": owner,
                    "lease_expires_at": lease_expires_at,
                    "updated_at": now,
                    "id": row["id"],
                    "pending": JOB_PENDING,
                },
            )
        return RUC(str(row["ruc"]))

    def reset_expired_leases(self) -> int:
        """Requeue in_progress jobs whose lease has elapsed (crashed owners).

        Lease timestamps are timezone-aware UTC isoformat, so the lexical
        comparison against `now` is a valid time comparison. A live owner
        renews nothing; the lease just has to outlast the worst-case single
        lookup, so an expired lease means the owner is gone.
        """
        now = _now()
        with self._transaction():
            cursor = self._conn.execute(
                RESET_EXPIRED_LEASES,
                {
                    "pending": JOB_PENDING,
                    "in_progress": JOB_IN_PROGRESS,
                    "updated_at": now,
                    "now": now,
                },
            )
        return cursor.rowcount

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
            in_progress=totals.get(JOB_IN_PROGRESS, 0),
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

    def _create_schema(self) -> None:
        self._conn.executescript(SCHEMA_DDL)


def state_path_for_output(output_csv: Path) -> Path:
    return output_csv.with_suffix(".state.sqlite3")


def _now() -> str:
    return datetime.now(UTC).isoformat()
