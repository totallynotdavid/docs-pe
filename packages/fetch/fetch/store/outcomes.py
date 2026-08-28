from __future__ import annotations

import sqlite3

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from socket import gethostname
from typing import TYPE_CHECKING, Self

from fetch.domain.policy import MAX_TOTAL_ATTEMPTS
from fetch.domain.types import Status
from fetch.pipeline.breaker import BreakerState
from fetch.store.payload import decode_rows, encode_rows


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from fetch.domain.types import Result, Row


# `ok` and `not_found` are immediately terminal. Failures retire at the cap.
_TERMINAL_PREDICATE = "status IN ('ok', 'not_found') OR attempt_count >= :cap"

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS outcomes (
    site          TEXT NOT NULL,
    doc           TEXT NOT NULL,
    status        TEXT NOT NULL,
    payload       TEXT NOT NULL DEFAULT '[]',
    error_code    TEXT NOT NULL DEFAULT '',
    error_detail  TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    session_id    TEXT NOT NULL DEFAULT '',
    proxy_id      TEXT NOT NULL DEFAULT '',
    provider      TEXT NOT NULL DEFAULT '',
    finished_at   TEXT NOT NULL,
    PRIMARY KEY (site, doc)
);
CREATE TABLE IF NOT EXISTS breaker_states (
    source               TEXT NOT NULL,
    provider             TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL,
    level                INTEGER NOT NULL,
    open_until           TEXT,
    updated_at           TEXT NOT NULL,
    PRIMARY KEY (source, provider)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    input_path   TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    output_path  TEXT NOT NULL,
    sites        TEXT NOT NULL,
    providers    TEXT NOT NULL,
    host         TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT NOT NULL DEFAULT ''
);
"""

# Success is terminal, clears error details, and cannot be overwritten by failure.
UPSERT_SUCCESS = """
INSERT INTO outcomes
    (site, doc, status, payload, error_code, error_detail,
     attempt_count, session_id, proxy_id, provider, finished_at)
VALUES
    (:site, :doc, 'ok', :payload, '', '',
     0, :session_id, :proxy_id, :provider, :finished_at)
ON CONFLICT(site, doc) DO UPDATE SET
    status = 'ok',
    payload = excluded.payload,
    error_code = '',
    error_detail = '',
    session_id = excluded.session_id,
    proxy_id = excluded.proxy_id,
    provider = excluded.provider,
    finished_at = excluded.finished_at
"""

UPSERT_FAILURE = """
INSERT INTO outcomes
    (site, doc, status, error_code, error_detail,
     attempt_count, session_id, proxy_id, provider, finished_at)
VALUES
    (:site, :doc, 'failed', :error_code, :error_detail,
     :attempt_count, :session_id, :proxy_id, :provider, :finished_at)
ON CONFLICT(site, doc) DO UPDATE SET
    status = 'failed',
    error_code = excluded.error_code,
    error_detail = excluded.error_detail,
    attempt_count = outcomes.attempt_count + excluded.attempt_count,
    session_id = excluded.session_id,
    proxy_id = excluded.proxy_id,
    provider = excluded.provider,
    finished_at = excluded.finished_at
WHERE outcomes.status NOT IN ('ok', 'not_found')
"""

UPSERT_NOT_FOUND = """
INSERT INTO outcomes
    (site, doc, status, payload, error_code, error_detail,
     attempt_count, session_id, proxy_id, provider, finished_at)
VALUES
    (:site, :doc, 'not_found', '[]', '', '',
     0, :session_id, :proxy_id, :provider, :finished_at)
ON CONFLICT(site, doc) DO UPDATE SET
    status = 'not_found',
    payload = '[]',
    error_code = '',
    error_detail = '',
    session_id = excluded.session_id,
    proxy_id = excluded.proxy_id,
    provider = excluded.provider,
    finished_at = excluded.finished_at
WHERE outcomes.status != 'ok'
"""

SELECT_DONE_PAIRS = f"SELECT site, doc FROM outcomes WHERE {_TERMINAL_PREDICATE}"

SELECT_SUCCESS_ROWS = """
SELECT doc, payload FROM outcomes
 WHERE site = :site AND status = 'ok'
 ORDER BY doc
"""

SELECT_ERROR_ROWS = """
SELECT doc, error_code, error_detail, attempt_count, session_id, proxy_id, provider, finished_at
  FROM outcomes
 WHERE site = :site AND status = 'failed'
 ORDER BY doc
"""

SELECT_NOT_FOUND_ROWS = """
SELECT doc, finished_at FROM outcomes
 WHERE site = :site AND status = 'not_found'
 ORDER BY doc
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


@dataclass(frozen=True)
class OutcomeRecord:
    site: str
    doc: str
    status: Status
    rows: tuple[Row, ...]
    error_code: str
    error_detail: str
    attempt_count: int
    session_id: str
    proxy_id: str
    provider: str
    finished_at: str


class OutcomeStore:
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
        self._conn.executescript(SCHEMA_DDL)
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def record_success(self, result: Result) -> None:
        self._write_success(
            site=result.site,
            doc=str(result.doc),
            rows=result.rows,
            session_id=result.http_session_id,
            proxy_id=result.proxy_id,
            provider=result.provider,
        )

    def record_import(self, *, site: str, doc: str, rows: tuple[Row, ...]) -> None:
        self._write_success(
            site=site,
            doc=doc,
            rows=rows,
            session_id="",
            proxy_id="",
            provider="",
        )

    def record_not_found(self, result: Result) -> None:
        with self._transaction():
            self._conn.execute(
                UPSERT_NOT_FOUND,
                {
                    "site": result.site,
                    "doc": str(result.doc),
                    "session_id": result.http_session_id,
                    "proxy_id": result.proxy_id,
                    "provider": result.provider,
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
                    "doc": str(result.doc),
                    "error_code": result.error_code,
                    "error_detail": result.error_detail,
                    "attempt_count": increment,
                    "session_id": result.http_session_id,
                    "proxy_id": result.proxy_id,
                    "provider": result.provider,
                    "finished_at": _now(),
                },
            )

    def start_run(
        self,
        *,
        run_id: str,
        input_path: Path,
        output_path: Path,
        sites: tuple[str, ...],
        providers: tuple[str, ...],
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO runs
                (run_id, input_path, input_sha256, output_path, sites, providers,
                 host, started_at)
            VALUES
                (:run_id, :input_path, :input_sha256, :output_path, :sites,
                 :providers, :host, :started_at)
            """,
            {
                "run_id": run_id,
                "input_path": str(input_path),
                "input_sha256": _sha256(input_path),
                "output_path": str(output_path),
                "sites": ",".join(sites),
                "providers": ",".join(providers),
                "host": gethostname(),
                "started_at": _now(),
            },
        )

    def finish_run(self, run_id: str) -> None:
        self._conn.execute(
            "UPDATE runs SET finished_at = :finished_at WHERE run_id = :run_id",
            {"run_id": run_id, "finished_at": _now()},
        )

    def breaker_state(self, *, source: str, provider: str) -> BreakerState | None:
        row = self._conn.execute(
            """
            SELECT source, provider, consecutive_failures, level, open_until
              FROM breaker_states
             WHERE source = :source AND provider = :provider
            """,
            {"source": source, "provider": provider},
        ).fetchone()
        if row is None:
            return None

        open_until = str(row["open_until"])
        return BreakerState(
            source=str(row["source"]),
            provider=str(row["provider"]),
            consecutive_failures=int(row["consecutive_failures"]),
            level=int(row["level"]),
            open_until=datetime.fromisoformat(open_until) if open_until else None,
        )

    def record_breaker(self, state: BreakerState) -> None:
        self._conn.execute(
            """
            INSERT INTO breaker_states
                (source, provider, consecutive_failures, level, open_until, updated_at)
            VALUES
                (:source, :provider, :consecutive_failures, :level, :open_until, :updated_at)
            ON CONFLICT(source, provider) DO UPDATE SET
                consecutive_failures = excluded.consecutive_failures,
                level = excluded.level,
                open_until = excluded.open_until,
                updated_at = excluded.updated_at
            """,
            {
                "source": state.source,
                "provider": state.provider,
                "consecutive_failures": state.consecutive_failures,
                "level": state.level,
                "open_until": state.open_until.isoformat() if state.open_until else "",
                "updated_at": _now(),
            },
        )

    def done_pairs(
        self,
        *,
        retry_cap: int = MAX_TOTAL_ATTEMPTS,
    ) -> set[tuple[str, str]]:
        rows = self._conn.execute(
            SELECT_DONE_PAIRS,
            {"cap": retry_cap},
        ).fetchall()

        return {(str(row["site"]), str(row["doc"])) for row in rows}

    def counts(
        self,
        site: str,
        *,
        retry_cap: int = MAX_TOTAL_ATTEMPTS,
    ) -> OutcomeCounts:
        succeeded = int(
            self._conn.execute(
                COUNT_SUCCEEDED,
                {"site": site},
            ).fetchone()["total"]
        )

        not_found = int(
            self._conn.execute(
                COUNT_NOT_FOUND,
                {"site": site},
            ).fetchone()["total"]
        )

        terminal_failed = int(
            self._conn.execute(
                COUNT_TERMINAL,
                {"site": site, "cap": retry_cap},
            ).fetchone()["total"]
        )

        retryable = int(
            self._conn.execute(
                COUNT_RETRYABLE,
                {"site": site, "cap": retry_cap},
            ).fetchone()["total"]
        )

        return OutcomeCounts(
            succeeded=succeeded,
            not_found=not_found,
            terminal_failed=terminal_failed,
            retryable=retryable,
        )

    def success_rows(self, site: str) -> Iterator[tuple[str, tuple[Row, ...]]]:
        for row in self._conn.execute(SELECT_SUCCESS_ROWS, {"site": site}):
            yield str(row["doc"]), decode_rows(str(row["payload"]))

    def not_found_rows(self, site: str) -> Iterator[list[str]]:
        for row in self._conn.execute(SELECT_NOT_FOUND_ROWS, {"site": site}):
            yield [
                str(row["doc"]),
                str(row["finished_at"]),
            ]

    def error_rows(self, site: str) -> Iterator[list[str]]:
        for row in self._conn.execute(SELECT_ERROR_ROWS, {"site": site}):
            yield [
                str(row["doc"]),
                str(row["error_code"]),
                str(row["error_detail"]),
                str(row["attempt_count"]),
                str(row["session_id"]),
                str(row["proxy_id"]),
                str(row["provider"]),
                str(row["finished_at"]),
            ]

    def outcomes(self) -> Iterator[OutcomeRecord]:
        rows = self._conn.execute(
            """
            SELECT site, doc, status, payload, error_code, error_detail, attempt_count,
                   session_id, proxy_id, provider, finished_at
              FROM outcomes
             ORDER BY site, doc
            """
        )
        for row in rows:
            yield OutcomeRecord(
                site=str(row["site"]),
                doc=str(row["doc"]),
                status=Status(str(row["status"])),
                rows=decode_rows(str(row["payload"])),
                error_code=str(row["error_code"]),
                error_detail=str(row["error_detail"]),
                attempt_count=int(row["attempt_count"]),
                session_id=str(row["session_id"]),
                proxy_id=str(row["proxy_id"]),
                provider=str(row["provider"]),
                finished_at=str(row["finished_at"]),
            )

    def record_snapshot(self, outcome: OutcomeRecord) -> None:
        with self._transaction():
            self._conn.execute(
                """
                INSERT INTO outcomes
                    (site, doc, status, payload, error_code, error_detail,
                     attempt_count, session_id, proxy_id, provider, finished_at)
                VALUES
                    (:site, :doc, :status, :payload, :error_code, :error_detail,
                     :attempt_count, :session_id, :proxy_id, :provider, :finished_at)
                """,
                {
                    "site": outcome.site,
                    "doc": outcome.doc,
                    "status": outcome.status.value,
                    "payload": encode_rows(outcome.rows),
                    "error_code": outcome.error_code,
                    "error_detail": outcome.error_detail,
                    "attempt_count": outcome.attempt_count,
                    "session_id": outcome.session_id,
                    "proxy_id": outcome.proxy_id,
                    "provider": outcome.provider,
                    "finished_at": outcome.finished_at,
                },
            )

    def _write_success(
        self,
        *,
        site: str,
        doc: str,
        rows: tuple[Row, ...],
        session_id: str,
        proxy_id: str,
        provider: str,
    ) -> None:
        with self._transaction():
            self._conn.execute(
                UPSERT_SUCCESS,
                {
                    "site": site,
                    "doc": doc,
                    "payload": encode_rows(rows),
                    "session_id": session_id,
                    "proxy_id": proxy_id,
                    "provider": provider,
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

    def _migrate(self) -> None:
        columns = {
            str(row["name"])
            for row in self._conn.execute("PRAGMA table_info(outcomes)")
        }
        if "provider" not in columns:
            self._conn.execute(
                "ALTER TABLE outcomes ADD COLUMN provider TEXT NOT NULL DEFAULT ''"
            )


def state_path_for_output(output_csv: Path) -> Path:
    return output_csv.with_suffix(".state.sqlite3")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file_obj:
        for block in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
