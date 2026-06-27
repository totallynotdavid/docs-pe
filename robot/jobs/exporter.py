from __future__ import annotations

import csv
import os

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from robot.jobs.store import JobStore


SUCCESS_HEADERS = ["ruc", "carrier", "lines", "total_lines"]
ERROR_HEADERS = [
    "ruc",
    "error_code",
    "error_detail",
    "attempt",
    "session_id",
    "proxy_id",
    "timestamp",
]


def export_csv(*, store: JobStore, output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(output_csv, SUCCESS_HEADERS, store.result_rows())
    _write_atomic(
        output_csv.with_suffix(".errors.csv"), ERROR_HEADERS, store.error_rows()
    )


def _write_atomic(
    path: Path,
    headers: list[str],
    rows: Iterable[Sequence[str | int]],
) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", newline="", encoding="utf-8") as file_obj:
        writer = csv.writer(file_obj)
        writer.writerow(headers)
        writer.writerows(rows)
        file_obj.flush()
        os.fsync(file_obj.fileno())
    temp_path.replace(path)
