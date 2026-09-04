from __future__ import annotations

import csv
import json
import sqlite3

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self


if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from capture.sites.base import Row

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS observations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    site          TEXT NOT NULL,
    ruc           TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('ok', 'rejected', 'failed')),
    payload       TEXT NOT NULL DEFAULT '{}',
    error_detail  TEXT NOT NULL DEFAULT '',
    observed_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observations_site_ruc_id
    ON observations (site, ruc, id DESC);
"""

LATEST_SUCCESS_SQL = """
SELECT observation.ruc, observation.payload, observation.observed_at
FROM observations AS observation
JOIN (
    SELECT ruc, MAX(id) AS id
    FROM observations
    WHERE status = 'ok' AND site = :site
    GROUP BY ruc
) AS latest ON latest.id = observation.id
ORDER BY observation.ruc
"""


class ObservationStore:
    """Persist lookup observations and export each site's latest successes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self._db = sqlite3.connect(
            path,
            timeout=30,
            isolation_level=None,
        )
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.execute("PRAGMA synchronous = NORMAL")
        self._db.execute("PRAGMA busy_timeout = 30000")
        self._db.executescript(SCHEMA_DDL)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self._db.close()

    def record_success(
        self,
        *,
        run_id: str,
        site: str,
        ruc: str,
        columns: dict[str, str],
    ) -> None:
        with self._transaction():
            self._db.execute(
                """
                INSERT INTO observations
                    (run_id, site, ruc, status, payload, observed_at)
                VALUES (:run_id, :site, :ruc, 'ok', :payload, :observed_at)
                """,
                {
                    "run_id": run_id,
                    "site": site,
                    "ruc": ruc,
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
        ruc: str,
        status: str,
        error_detail: str,
    ) -> None:
        if status not in {"rejected", "failed"}:
            msg = f"invalid failure status: {status}"
            raise ValueError(msg)

        with self._transaction():
            self._db.execute(
                """
                INSERT INTO observations
                    (run_id, site, ruc, status, error_detail, observed_at)
                VALUES (:run_id, :site, :ruc, :status, :error_detail, :observed_at)
                """,
                {
                    "run_id": run_id,
                    "site": site,
                    "ruc": ruc,
                    "status": status,
                    "error_detail": error_detail,
                    "observed_at": _now(),
                },
            )

    def latest(self, site: str, ruc: str) -> dict[str, str] | None:
        row = self._db.execute(
            """
            SELECT payload
            FROM observations
            WHERE site = :site AND ruc = :ruc AND status = 'ok'
            ORDER BY id DESC
            LIMIT 1
            """,
            {"site": site, "ruc": ruc},
        ).fetchone()

        if row is None:
            return None

        return json.loads(row["payload"])

    def export_current(
        self,
        path: Path,
        *,
        site: str,
        header: tuple[str, ...],
        project: Callable[[str, dict[str, str], str], Row],
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.tmp")

        with temp_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file, lineterminator="\n")
            writer.writerow(header)

            for row in self._db.execute(
                LATEST_SUCCESS_SQL,
                {"site": site},
            ):
                writer.writerow(
                    project(
                        row["ruc"],
                        json.loads(row["payload"]),
                        row["observed_at"],
                    )
                )

        temp_path.replace(path)

    def observation_count(self) -> int:
        return int(
            self._db.execute("SELECT COUNT(*) AS total FROM observations").fetchone()[
                "total"
            ]
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")

        try:
            yield
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")


def _now() -> str:
    return datetime.now(UTC).isoformat()
