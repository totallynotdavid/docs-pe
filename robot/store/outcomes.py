from __future__ import annotations

import sqlite3

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

from robot.domain.policy import MAX_TOTAL_ATTEMPTS
from robot.store.payload import decode_rows, encode_rows


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from robot.domain.types import Result, Row


# Implements the retirement rule owned by domain/policy.py (MAX_TOTAL_ATTEMPTS).
# not_found is terminal on first contact, same as ok, never by attempt count.
_TERMINAL_PREDICATE = "status IN ('ok', 'not_found') OR attempt_count >= :cap"

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS outcomes (
    site          TEXT NOT NULL,
    ruc           TEXT NOT NULL,
    status        TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '[]',
    error_code    TEXT NOT NULL DEFAULT '',
    error_detail  TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    session_id    TEXT NOT NULL DEFAULT '',
    proxy_id      TEXT NOT NULL DEFAULT '',
    finished_at   TEXT NOT NULL,
    PRIMARY KEY (site, ruc)
);
"""

# A pair is one row that flips ok/failed. Success is terminal and clears the error
# fields. Failure accumulates attempts and never downgrades a pair that succeeded.
UPSERT_SUCCESS = """
INSERT INTO outcomes
    (site, ruc, status, payload, error_code, error_detail,
     attempt_count, session_id, proxy_id, finished_at)
VALUES
    (:site, :ruc, 'ok', :payload, '', '',
     0, :session_id, :proxy_id, :finished_at)
ON CONFLICT(site, ruc) DO UPDATE SET
    status = 'ok',
    payload = excluded.payload,
    error_code = '',
    error_detail = '',
    session_id = excluded.session_id,
    proxy_id = excluded.proxy_id,
    finished_at = excluded.finished_at
"""

UPSERT_FAILURE = """
INSERT INTO outcomes
    (site, ruc, status, error_code, error_detail,
     attempt_count, session_id, proxy_id, finished_at)
VALUES
    (:site, :ruc, 'failed', :error_code, :error_detail,
     :attempt_count, :session_id, :proxy_id, :finished_at)
ON CONFLICT(site, ruc) DO UPDATE SET
    status = 'failed',
    error_code = excluded.error_code,
    error_detail = excluded.error_detail,
    attempt_count = outcomes.attempt_count + excluded.attempt_count,
    session_id = excluded.session_id,
    proxy_id = excluded.proxy_id,
    finished_at = excluded.finished_at
WHERE outcomes.status NOT IN ('ok', 'not_found')
"""

UPSERT_NOT_FOUND = """
INSERT INTO outcomes
    (site, ruc, status, payload, error_code, error_detail,
     attempt_count, session_id, proxy_id, finished_at)
VALUES
    (:site, :ruc, 'not_found', '[]', '', '',
     0, :session_id, :proxy_id, :finished_at)
ON CONFLICT(site, ruc) DO UPDATE SET
    status = 'not_found',
    payload = '[]',
    error_code = '',
    error_detail = '',
    session_id = excluded.session_id,
    proxy_id = excluded.proxy_id,
    finished_at = excluded.finished_at
WHERE outcomes.status != 'ok'
"""

SELECT_DONE_PAIRS = f"SELECT site, ruc FROM outcomes WHERE {_TERMINAL_PREDICATE}"

SELECT_SUCCESS_ROWS = """
SELECT ruc, payload FROM outcomes
 WHERE site = :site AND status = 'ok'
 ORDER BY ruc
"""

SELECT_ERROR_ROWS = """
SELECT ruc, error_code, error_detail, attempt_count, session_id, proxy_id, finished_at
  FROM outcomes
 WHERE site = :site AND status = 'failed'
 ORDER BY ruc
"""

SELECT_NOT_FOUND_ROWS = """
SELECT ruc, finished_at FROM outcomes
 WHERE site = :site AND status = 'not_found'
 ORDER BY ruc
"""

COUNT_SUCCEEDED = (
    "SELECT COUNT(*) AS total FROM outcomes WHERE site = :site AND status = 'ok'"
)
COUNT_NOT_FOUND = (
    "SELECT COUNT(*) AS total FROM outcomes WHERE site = :site AND status = 'not_found'"
)
COUNT_TERMINAL = (
    "SELECT COUNT(*) AS total FROM outcomes "
    "WHERE site = :site AND status = 'failed' AND attempt_count >= :cap"
)
COUNT_RETRYABLE = (
    "SELECT COUNT(*) AS total FROM outcomes "
    "WHERE site = :site AND status = 'failed' AND attempt_count < :cap"
)


@dataclass(frozen=True)
class OutcomeCounts:
    succeeded: int
    not_found: int
    terminal_failed: int
    retryable: int


class OutcomeStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._conn.executescript(SCHEMA_DDL)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def record_success(self, result: Result) -> None:
        self._write_success(
            site=result.site,
            ruc=str(result.ruc),
            rows=result.rows,
            session_id=result.http_session_id,
            proxy_id=result.proxy_id,
        )

    def record_import(self, *, site: str, ruc: str, rows: tuple[Row, ...]) -> None:
        self._write_success(site=site, ruc=ruc, rows=rows, session_id="", proxy_id="")

    def record_not_found(self, result: Result) -> None:
        with self._transaction():
            self._conn.execute(
                UPSERT_NOT_FOUND,
                {
                    "site": result.site,
                    "ruc": str(result.ruc),
                    "session_id": result.http_session_id,
                    "proxy_id": result.proxy_id,
                    "finished_at": _now(),
                },
            )

    def record_failure(self, result: Result) -> None:
        # Breaker-open attempts do not count toward retirement.
        increment = result.attempt if result.made_healthy_contact else 0
        with self._transaction():
            self._conn.execute(
                UPSERT_FAILURE,
                {
                    "site": result.site,
                    "ruc": str(result.ruc),
                    "error_code": result.error_code,
                    "error_detail": result.error_detail,
                    "attempt_count": increment,
                    "session_id": result.http_session_id,
                    "proxy_id": result.proxy_id,
                    "finished_at": _now(),
                },
            )

    def done_pairs(
        self, *, retry_cap: int = MAX_TOTAL_ATTEMPTS
    ) -> set[tuple[str, str]]:
        rows = self._conn.execute(SELECT_DONE_PAIRS, {"cap": retry_cap}).fetchall()
        return {(str(row["site"]), str(row["ruc"])) for row in rows}

    def counts(
        self, site: str, *, retry_cap: int = MAX_TOTAL_ATTEMPTS
    ) -> OutcomeCounts:
        succeeded = int(
            self._conn.execute(COUNT_SUCCEEDED, {"site": site}).fetchone()["total"]
        )
        not_found = int(
            self._conn.execute(COUNT_NOT_FOUND, {"site": site}).fetchone()["total"]
        )
        terminal = int(
            self._conn.execute(
                COUNT_TERMINAL, {"site": site, "cap": retry_cap}
            ).fetchone()["total"]
        )
        retryable = int(
            self._conn.execute(
                COUNT_RETRYABLE, {"site": site, "cap": retry_cap}
            ).fetchone()["total"]
        )
        return OutcomeCounts(
            succeeded=succeeded,
            not_found=not_found,
            terminal_failed=terminal,
            retryable=retryable,
        )

    def success_rows(self, site: str) -> Iterator[tuple[str, tuple[Row, ...]]]:
        for row in self._conn.execute(SELECT_SUCCESS_ROWS, {"site": site}):
            yield str(row["ruc"]), decode_rows(str(row["payload"]))

    def not_found_rows(self, site: str) -> Iterator[list[str]]:
        for row in self._conn.execute(SELECT_NOT_FOUND_ROWS, {"site": site}):
            yield [str(row["ruc"]), str(row["finished_at"])]

    def error_rows(self, site: str) -> Iterator[list[str]]:
        for row in self._conn.execute(SELECT_ERROR_ROWS, {"site": site}):
            yield [
                str(row["ruc"]),
                str(row["error_code"]),
                str(row["error_detail"]),
                str(row["attempt_count"]),
                str(row["session_id"]),
                str(row["proxy_id"]),
                str(row["finished_at"]),
            ]

    def _write_success(
        self,
        *,
        site: str,
        ruc: str,
        rows: tuple[Row, ...],
        session_id: str,
        proxy_id: str,
    ) -> None:
        with self._transaction():
            self._conn.execute(
                UPSERT_SUCCESS,
                {
                    "site": site,
                    "ruc": ruc,
                    "payload": encode_rows(rows),
                    "session_id": session_id,
                    "proxy_id": proxy_id,
                    "finished_at": _now(),
                },
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


def state_path_for_output(output_csv: Path) -> Path:
    return output_csv.with_suffix(".state.sqlite3")


def _now() -> str:
    return datetime.now(UTC).isoformat()
