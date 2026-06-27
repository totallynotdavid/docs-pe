from __future__ import annotations

import csv
import sqlite3

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from robot.domain.types import RUC, CarrierCount
from robot.store.export import SUCCESS_HEADERS


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from robot.domain.types import LookupResult


# Done RUCs are the union of successful results and terminal errors.
SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS results (
    ruc TEXT NOT NULL,
    carrier TEXT NOT NULL,
    lines INTEGER NOT NULL,
    total_lines INTEGER NOT NULL,
    PRIMARY KEY (ruc, carrier)
);

CREATE TABLE IF NOT EXISTS errors (
    ruc TEXT NOT NULL PRIMARY KEY,
    error_code TEXT NOT NULL DEFAULT '',
    error_detail TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    session_id TEXT NOT NULL DEFAULT '',
    proxy_id TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL
);
"""

DELETE_RESULTS_FOR_RUC = "DELETE FROM results WHERE ruc = :ruc"
DELETE_ERROR_FOR_RUC = "DELETE FROM errors WHERE ruc = :ruc"

INSERT_RESULT = """
INSERT INTO results (ruc, carrier, lines, total_lines)
VALUES (:ruc, :carrier, :lines, :total_lines)
"""

# A re-attempt that finally fails overwrites the prior failure for that RUC, so
# the row always reflects the last attempt and its cumulative count.
UPSERT_ERROR = """
INSERT INTO errors
    (ruc, error_code, error_detail, attempt_count, session_id, proxy_id, finished_at)
VALUES
    (:ruc, :error_code, :error_detail, :attempt_count, :session_id, :proxy_id, :finished_at)
ON CONFLICT(ruc) DO UPDATE SET
    error_code = excluded.error_code,
    error_detail = excluded.error_detail,
    attempt_count = excluded.attempt_count,
    session_id = excluded.session_id,
    proxy_id = excluded.proxy_id,
    finished_at = excluded.finished_at
"""

SELECT_DONE_RUCS = "SELECT ruc FROM results UNION SELECT ruc FROM errors"

SELECT_RESULT_ROWS = """
SELECT ruc, carrier, lines, total_lines
  FROM results
 ORDER BY ruc, carrier
"""

SELECT_ERROR_ROWS = """
SELECT ruc, error_code, error_detail, attempt_count, session_id, proxy_id, finished_at
  FROM errors
 ORDER BY ruc
"""

COUNT_SUCCEEDED = "SELECT COUNT(DISTINCT ruc) AS total FROM results"
COUNT_FAILED = "SELECT COUNT(*) AS total FROM errors"


@dataclass(frozen=True)
class OutcomeCounts:
    succeeded: int
    failed: int


class OutcomeLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._conn.executescript(SCHEMA_DDL)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> OutcomeLog:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def record_success(self, result: LookupResult) -> None:
        ruc = str(result.ruc)
        carriers = result.carrier_counts or (
            CarrierCount(carrier="unknown", lines=result.total_lines),
        )
        with self._transaction():
            self._conn.execute(DELETE_ERROR_FOR_RUC, {"ruc": ruc})
            self._conn.execute(DELETE_RESULTS_FOR_RUC, {"ruc": ruc})
            self._conn.executemany(
                INSERT_RESULT,
                [
                    {
                        "ruc": ruc,
                        "carrier": item.carrier,
                        "lines": item.lines,
                        "total_lines": result.total_lines,
                    }
                    for item in carriers
                ],
            )

    def record_failure(self, result: LookupResult) -> None:
        with self._transaction():
            self._conn.execute(
                UPSERT_ERROR,
                {
                    "ruc": str(result.ruc),
                    "error_code": result.error_code,
                    "error_detail": result.error_detail,
                    "attempt_count": result.attempt,
                    "session_id": result.http_session_id,
                    "proxy_id": result.proxy_id,
                    "finished_at": _now(),
                },
            )

    def done_rucs(self) -> set[str]:
        rows = self._conn.execute(SELECT_DONE_RUCS).fetchall()
        return {str(row["ruc"]) for row in rows}

    def counts(self) -> OutcomeCounts:
        succeeded = int(self._conn.execute(COUNT_SUCCEEDED).fetchone()["total"])
        failed = int(self._conn.execute(COUNT_FAILED).fetchone()["total"])
        return OutcomeCounts(succeeded=succeeded, failed=failed)

    def result_rows(self) -> Iterator[list[str | int]]:
        for row in self._conn.execute(SELECT_RESULT_ROWS):
            yield [
                str(row["ruc"]),
                str(row["carrier"]),
                int(row["lines"]),
                int(row["total_lines"]),
            ]

    def error_rows(self) -> Iterator[list[str]]:
        for row in self._conn.execute(SELECT_ERROR_ROWS):
            yield [
                str(row["ruc"]),
                str(row["error_code"]),
                str(row["error_detail"]),
                str(row["attempt_count"]),
                str(row["session_id"]),
                str(row["proxy_id"]),
                str(row["finished_at"]),
            ]

    def import_csv(self, success_csv: Path) -> int:
        """Import successes from a prior export."""
        grouped, totals = _read_success_csv(success_csv)
        if not grouped:
            return 0
        with self._transaction():
            for ruc, carriers in grouped.items():
                self._conn.executemany(
                    INSERT_RESULT,
                    [
                        {
                            "ruc": ruc,
                            "carrier": item.carrier,
                            "lines": item.lines,
                            "total_lines": totals[ruc],
                        }
                        for item in carriers
                    ],
                )
        return len(grouped)

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


def state_path_for_output(output_csv: Path) -> Path:
    return output_csv.with_suffix(".state.sqlite3")


def _read_success_csv(
    path: Path,
) -> tuple[dict[str, list[CarrierCount]], dict[str, int]]:
    if not path.exists() or path.stat().st_size == 0:
        return {}, {}

    grouped: dict[str, list[CarrierCount]] = {}
    totals: dict[str, int] = {}
    with path.open(newline="", encoding="utf-8") as file_obj:
        reader = csv.reader(file_obj)
        header = next(reader, [])
        if header != SUCCESS_HEADERS:
            return {}, {}
        for row in reader:
            if len(row) != len(SUCCESS_HEADERS):
                continue
            ruc_raw, carrier, lines_raw, total_raw = row
            try:
                ruc = str(RUC(ruc_raw))
                lines = int(lines_raw)
                total = int(total_raw)
            except (TypeError, ValueError):
                continue
            grouped.setdefault(ruc, []).append(
                CarrierCount(carrier=carrier, lines=lines)
            )
            totals[ruc] = total
    return grouped, totals


def _now() -> str:
    return datetime.now(UTC).isoformat()
