from __future__ import annotations

import csv
import os

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Sequence
    from pathlib import Path

    from fetch.domain.types import Projection, Site
    from fetch.store.outcomes import OutcomeStore

ERROR_HEADERS = [
    "doc",
    "error_code",
    "error_detail",
    "attempt",
    "session_id",
    "proxy_id",
    "provider",
    "timestamp",
]
NOT_FOUND_HEADERS = ["doc", "timestamp"]


def export_all(
    *,
    store: OutcomeStore,
    output_csv: Path,
    sites: Sequence[Site],
) -> None:
    for site in sites:
        export_site(store=store, output_csv=output_csv, site=site)


def export_site(*, store: OutcomeStore, output_csv: Path, site: Site) -> None:
    success_path = site_csv_path(output_csv, site.name)
    success_path.parent.mkdir(parents=True, exist_ok=True)

    _write_atomic(
        success_path,
        ["doc", *site.columns],
        _success_lines(store, site.name),
    )

    for projection in site.projections:
        _write_atomic(
            _projection_path(success_path, projection.name),
            ["doc", *projection.columns],
            _projection_lines(store, site.name, projection),
        )

    _write_atomic(
        success_path.with_suffix(".errors.csv"),
        ERROR_HEADERS,
        store.error_rows(site.name),
    )

    _write_atomic(
        success_path.with_suffix(".not_found.csv"),
        NOT_FOUND_HEADERS,
        store.not_found_rows(site.name),
    )


def site_csv_path(output_csv: Path, site_name: str) -> Path:
    return output_csv.with_name(f"{output_csv.stem}.{site_name}{output_csv.suffix}")


def _projection_path(success_path: Path, projection_name: str) -> Path:
    return success_path.with_name(
        f"{success_path.stem}.{projection_name}{success_path.suffix}"
    )


def _success_lines(
    store: OutcomeStore,
    site_name: str,
) -> Iterator[list[str | int]]:
    for doc, rows in store.success_rows(site_name):
        for row in rows:
            yield [doc, *row]


def _projection_lines(
    store: OutcomeStore,
    site_name: str,
    projection: Projection,
) -> Iterator[list[str | int]]:
    for doc, rows in store.success_rows(site_name):
        for row in projection.project(rows):
            yield [doc, *row]


def _write_atomic(
    path: Path,
    headers: Sequence[str],
    rows: Iterable[Sequence[str | int]],
) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")

    with temp_path.open("w", newline="", encoding="utf-8") as file_obj:
        writer = csv.writer(file_obj, lineterminator="\n")
        writer.writerow(headers)
        writer.writerows(rows)

        file_obj.flush()
        os.fsync(file_obj.fileno())

    temp_path.replace(path)
