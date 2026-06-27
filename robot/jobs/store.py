from __future__ import annotations

import csv
import sqlite3

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
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

PROVIDER_VERSION = "osiptel-v1"


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
            """
            INSERT OR IGNORE INTO jobs (ruc, status, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            (str(ruc), JOB_PENDING, now, now),
        )
        if cursor.rowcount > 0:
            return True

        cursor = self._conn.execute(
            """
            UPDATE jobs
               SET status = ?,
                   last_error_code = '',
                   last_error_detail = '',
                   updated_at = ?,
                   started_at = NULL,
                   finished_at = NULL
             WHERE ruc = ? AND status = ?
            """,
            (JOB_PENDING, now, str(ruc), JOB_FAILED),
        )
        return cursor.rowcount > 0

    def reset_running(self) -> int:
        now = _now()
        cursor = self._conn.execute(
            """
            UPDATE jobs
               SET status = ?, updated_at = ?, started_at = NULL
             WHERE status = ?
            """,
            (JOB_PENDING, now, JOB_RUNNING),
        )
        return cursor.rowcount

    def claim_next(self, *, worker_id: int) -> ClaimedJob | None:
        with self._transaction():
            row = self._conn.execute(
                """
                SELECT id, ruc, attempt_count
                  FROM jobs
                 WHERE status = ?
                 ORDER BY id
                 LIMIT 1
                """,
                (JOB_PENDING,),
            ).fetchone()
            if row is None:
                return None

            now = _now()
            attempt_no = int(row["attempt_count"]) + 1
            self._conn.execute(
                """
                UPDATE jobs
                   SET status = ?,
                       attempt_count = ?,
                       worker_id = ?,
                       started_at = ?,
                       updated_at = ?
                 WHERE id = ? AND status = ?
                """,
                (
                    JOB_RUNNING,
                    attempt_no,
                    worker_id,
                    now,
                    now,
                    int(row["id"]),
                    JOB_PENDING,
                ),
            )
            return ClaimedJob(
                id=int(row["id"]),
                ruc=RUC(str(row["ruc"])),
                attempt_no=attempt_no,
            )

    def complete_success(self, *, job: ClaimedJob, result: LookupResult) -> None:
        now = _now()
        with self._transaction():
            self._conn.execute("DELETE FROM results WHERE ruc = ?", (str(job.ruc),))
            carriers = result.carrier_counts or (
                CarrierCount(carrier="unknown", lines=result.total_lines),
            )
            self._conn.executemany(
                """
                INSERT INTO results (
                    ruc, carrier, lines, total_lines, fetched_at, provider_version
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(job.ruc),
                        item.carrier,
                        item.lines,
                        result.total_lines,
                        now,
                        PROVIDER_VERSION,
                    )
                    for item in carriers
                ],
            )
            self._conn.execute(
                """
                UPDATE jobs
                   SET status = ?,
                       last_error_code = '',
                       last_error_detail = '',
                       session_id = ?,
                       proxy_id = ?,
                       finished_at = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    JOB_SUCCEEDED,
                    result.session_id,
                    result.proxy_id,
                    now,
                    now,
                    job.id,
                ),
            )

    def complete_failure(self, *, job: ClaimedJob, result: LookupResult) -> None:
        now = _now()
        with self._transaction():
            self._conn.execute(
                """
                UPDATE jobs
                   SET status = ?,
                       last_error_code = ?,
                       last_error_detail = ?,
                       session_id = ?,
                       proxy_id = ?,
                       finished_at = ?,
                       updated_at = ?
                 WHERE id = ?
                """,
                (
                    JOB_FAILED,
                    result.error_code,
                    result.error_detail,
                    result.session_id,
                    result.proxy_id,
                    now,
                    now,
                    job.id,
                ),
            )

    def summary(self) -> StoreSummary:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) AS total FROM jobs GROUP BY status"
        ).fetchall()
        totals = {str(row["status"]): int(row["total"]) for row in rows}
        return StoreSummary(
            pending=totals.get(JOB_PENDING, 0),
            running=totals.get(JOB_RUNNING, 0),
            succeeded=totals.get(JOB_SUCCEEDED, 0),
            failed=totals.get(JOB_FAILED, 0),
        )

    def result_rows(self) -> Iterator[list[str | int]]:
        rows = self._conn.execute(
            """
            SELECT ruc, carrier, lines, total_lines
              FROM results
             ORDER BY ruc, carrier
            """
        )
        for row in rows:
            yield [
                str(row["ruc"]),
                str(row["carrier"]),
                int(row["lines"]),
                int(row["total_lines"]),
            ]

    def error_rows(self) -> Iterator[list[str]]:
        rows = self._conn.execute(
            """
            SELECT ruc,
                   last_error_code,
                   last_error_detail,
                   attempt_count,
                   session_id,
                   proxy_id,
                   finished_at
              FROM jobs
             WHERE status = ?
             ORDER BY ruc
            """,
            (JOB_FAILED,),
        )
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
            "SELECT id, attempt_count FROM jobs WHERE ruc = ?",
            (str(ruc),),
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
        self._conn.executescript(
            """
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
                finished_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_jobs_status_id ON jobs(status, id);

            CREATE TABLE IF NOT EXISTS results (
                ruc TEXT NOT NULL,
                carrier TEXT NOT NULL,
                lines INTEGER NOT NULL,
                total_lines INTEGER NOT NULL,
                fetched_at TEXT NOT NULL,
                provider_version TEXT NOT NULL,
                PRIMARY KEY (ruc, carrier),
                FOREIGN KEY (ruc) REFERENCES jobs(ruc) ON DELETE CASCADE
            );
            """
        )


def state_path_for_output(output_csv: Path) -> Path:
    return output_csv.with_suffix(".state.sqlite3")


def _now() -> str:
    return datetime.now(UTC).isoformat()
