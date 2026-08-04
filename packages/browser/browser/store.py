from __future__ import annotations

import csv
import json
import sqlite3

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Self


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    site          TEXT NOT NULL,
    subject       TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('ok', 'rejected', 'failed')),
    payload       TEXT NOT NULL DEFAULT '{}',
    error_detail  TEXT NOT NULL DEFAULT '',
    observed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_site_subject_id
    ON observations (site, subject, id DESC);
"""

LATEST_SUCCESS = """
SELECT observation.subject, observation.payload, observation.observed_at
FROM observations AS observation
JOIN (
    SELECT subject, MAX(id) AS id
    FROM observations
    WHERE status = 'ok' AND site = :site
    GROUP BY subject
) AS latest ON latest.id = observation.id
ORDER BY observation.subject
"""


class ObservationStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self._connection = sqlite3.connect(
            path,
            timeout=30,
            isolation_level=None,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA synchronous = NORMAL")
        self._connection.execute("PRAGMA busy_timeout = 30000")
        self._connection.executescript(SCHEMA_DDL)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self._connection.close()

    def record_success(
        self,
        *,
        run_id: str,
        site: str,
        subject: str,
        columns: dict[str, str],
    ) -> None:
        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO observations
                    (run_id, site, subject, status, payload, observed_at)
                VALUES (:run_id, :site, :subject, 'ok', :payload, :observed_at)
                """,
                {
                    "run_id": run_id,
                    "site": site,
                    "subject": subject,
                    "payload": json.dumps(
                        columns,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    "observed_at": _now(),
                },
            )

    def record_failure(
        self,
        *,
        run_id: str,
        site: str,
        subject: str,
        status: str,
        error_detail: str,
    ) -> None:
        if status not in {"rejected", "failed"}:
            msg = f"invalid failure status: {status}"
            raise ValueError(msg)

        with self._transaction():
            self._connection.execute(
                """
                INSERT INTO observations
                    (run_id, site, subject, status, error_detail, observed_at)
                VALUES (:run_id, :site, :subject, :status, :error_detail, :observed_at)
                """,
                {
                    "run_id": run_id,
                    "site": site,
                    "subject": subject,
                    "status": status,
                    "error_detail": error_detail,
                    "observed_at": _now(),
                },
            )

    def done_subjects(self, site: str) -> set[str]:
        rows = self._connection.execute(
            """
            SELECT DISTINCT subject
            FROM observations
            WHERE site = :site AND status = 'ok'
            """,
            {"site": site},
        ).fetchall()

        return {row["subject"] for row in rows}

    def latest(self, site: str, subject: str) -> dict[str, str] | None:
        row = self._connection.execute(
            """
            SELECT payload
            FROM observations
            WHERE site = :site AND subject = :subject AND status = 'ok'
            ORDER BY id DESC
            LIMIT 1
            """,
            {"site": site, "subject": subject},
        ).fetchone()

        if row is None:
            return None

        parsed: dict[str, str] = json.loads(row["payload"])
        return parsed

    def export_current(
        self,
        path: Path,
        *,
        site: str,
        header: tuple[str, ...],
        project: Callable[[str, dict[str, str], str], list[Any]],
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.tmp")

        with temp_path.open("w", newline="", encoding="utf-8") as file_obj:
            writer = csv.writer(file_obj)
            writer.writerow(header)

            for row in self._connection.execute(LATEST_SUCCESS, {"site": site}):
                columns: dict[str, str] = json.loads(row["payload"])
                writer.writerow(
                    project(
                        row["subject"],
                        columns,
                        row["observed_at"],
                    )
                )

        temp_path.replace(path)

    def observation_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS total FROM observations"
        ).fetchone()

        return int(row["total"])

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._connection.execute("BEGIN IMMEDIATE")

        try:
            yield
        except Exception:
            self._connection.execute("ROLLBACK")
            raise
        else:
            self._connection.execute("COMMIT")


def _now() -> str:
    return datetime.now(UTC).isoformat()
