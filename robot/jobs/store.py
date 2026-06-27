from __future__ import annotations

import csv
import sqlite3

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from robot.domain.types import RUC, CarrierCount, LookupResult, Status
from robot.jobs.csv_contract import SUCCESS_HEADERS


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"

# Upper bound on how long one job may stay claimed before another worker may
# reclaim it. It must comfortably exceed a single lookup including retries and
# ban cooldowns; a job whose worker died mid-flight becomes claimable again once
# its lease lapses, without waiting for the next process start. Promote to
# RunConfig if it ever needs to track wait/cooldown tuning.
LEASE_SECONDS = 900


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ruc TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    worker_id INTEGER,
    last_error_code TEXT NOT NULL DEFAULT '',
    last_error_detail TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    proxy_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    claimed_until TEXT
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
       started_at = NULL,
       finished_at = NULL
 WHERE ruc = :ruc AND status = :from_status
"""

RESET_RUNNING_JOBS = """
UPDATE jobs
   SET status = :status, updated_at = :updated_at, started_at = NULL
 WHERE status = :from_status
"""

# Claimable means a fresh pending job or a running job whose lease has lapsed
# (its worker died mid-flight). The enclosing BEGIN IMMEDIATE serializes claims,
# so the follow-up UPDATE keys on id alone without a compare-and-set on status.
SELECT_NEXT_PENDING = """
SELECT id, ruc, attempt_count
  FROM jobs
 WHERE status = :pending
    OR (status = :running AND claimed_until IS NOT NULL AND claimed_until <= :now)
 ORDER BY id
 LIMIT 1
"""

MARK_JOB_RUNNING = """
UPDATE jobs
   SET status = :status,
       attempt_count = :attempt_count,
       worker_id = :worker_id,
       started_at = :started_at,
       updated_at = :updated_at,
       claimed_until = :claimed_until
 WHERE id = :id
"""

DELETE_RESULTS_FOR_RUC = "DELETE FROM results WHERE ruc = :ruc"

INSERT_RESULT = """
INSERT INTO results (ruc, carrier, lines, total_lines)
VALUES (:ruc, :carrier, :lines, :total_lines)
"""

MARK_JOB_SUCCEEDED = """
UPDATE jobs
   SET status = :status,
       last_error_code = '',
       last_error_detail = '',
       session_id = :session_id,
       proxy_id = :proxy_id,
       finished_at = :finished_at,
       updated_at = :updated_at,
       claimed_until = NULL
 WHERE id = :id
"""

MARK_JOB_FAILED = """
UPDATE jobs
   SET status = :status,
       last_error_code = :last_error_code,
       last_error_detail = :last_error_detail,
       session_id = :session_id,
       proxy_id = :proxy_id,
       finished_at = :finished_at,
       updated_at = :updated_at,
       claimed_until = NULL
 WHERE id = :id
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

SELECT_JOB_BY_RUC = "SELECT id, attempt_count FROM jobs WHERE ruc = :ruc"


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    ruc: RUC
    attempt_no: int


@dataclass(frozen=True)
class StoreSummary:
    pending: int
    running: int
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

    def __exit__(self, *_) -> None:
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

    def reset_running(self) -> int:
        now = _now()
        cursor = self._conn.execute(
            RESET_RUNNING_JOBS,
            {
                "status": JOB_PENDING,
                "updated_at": now,
                "from_status": JOB_RUNNING,
            },
        )
        return cursor.rowcount

    def claim_next(self, *, worker_id: int) -> ClaimedJob | None:
        with self._transaction():
            now_dt = _now_dt()
            now = now_dt.isoformat()
            row = self._conn.execute(
                SELECT_NEXT_PENDING,
                {"pending": JOB_PENDING, "running": JOB_RUNNING, "now": now},
            ).fetchone()
            if row is None:
                return None

            attempt_no = int(row["attempt_count"]) + 1
            self._conn.execute(
                MARK_JOB_RUNNING,
                {
                    "status": JOB_RUNNING,
                    "attempt_count": attempt_no,
                    "worker_id": worker_id,
                    "started_at": now,
                    "updated_at": now,
                    "claimed_until": _lease_deadline(now_dt),
                    "id": int(row["id"]),
                },
            )
            return ClaimedJob(
                id=int(row["id"]),
                ruc=RUC(str(row["ruc"])),
                attempt_no=attempt_no,
            )

    def complete_success(self, *, job: ClaimedJob, result: LookupResult) -> None:
        now = _now()
        with self._transaction():
            self._conn.execute(DELETE_RESULTS_FOR_RUC, {"ruc": str(job.ruc)})
            carriers = result.carrier_counts or (
                CarrierCount(carrier="unknown", lines=result.total_lines),
            )
            self._conn.executemany(
                INSERT_RESULT,
                [
                    {
                        "ruc": str(job.ruc),
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
                    "session_id": result.session_id,
                    "proxy_id": result.proxy_id,
                    "finished_at": now,
                    "updated_at": now,
                    "id": job.id,
                },
            )

    def complete_failure(self, *, job: ClaimedJob, result: LookupResult) -> None:
        now = _now()
        with self._transaction():
            self._conn.execute(
                MARK_JOB_FAILED,
                {
                    "status": JOB_FAILED,
                    "last_error_code": result.error_code,
                    "last_error_detail": result.error_detail,
                    "session_id": result.session_id,
                    "proxy_id": result.proxy_id,
                    "finished_at": now,
                    "updated_at": now,
                    "id": job.id,
                },
            )

    def summary(self) -> StoreSummary:
        rows = self._conn.execute(SELECT_STATUS_COUNTS).fetchall()
        totals = {str(row["status"]): int(row["total"]) for row in rows}
        return StoreSummary(
            pending=totals.get(JOB_PENDING, 0),
            running=totals.get(JOB_RUNNING, 0),
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
            job = self._job_for_ruc(ruc)
            if job is None:
                continue
            result = LookupResult(
                ruc=ruc,
                status=Status.OK,
                total_lines=totals[ruc_raw],
                carrier_counts=tuple(carriers),
            )
            self.complete_success(job=job, result=result)
            seeded += 1
        return seeded

    def _job_for_ruc(self, ruc: RUC) -> ClaimedJob | None:
        row = self._conn.execute(
            SELECT_JOB_BY_RUC,
            {"ruc": str(ruc)},
        ).fetchone()
        if row is None:
            return None
        return ClaimedJob(
            id=int(row["id"]),
            ruc=ruc,
            attempt_no=int(row["attempt_count"]),
        )

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


def _now_dt() -> datetime:
    return datetime.now(UTC)


def _now() -> str:
    return _now_dt().isoformat()


def _lease_deadline(start: datetime) -> str:
    return (start + timedelta(seconds=LEASE_SECONDS)).isoformat()
