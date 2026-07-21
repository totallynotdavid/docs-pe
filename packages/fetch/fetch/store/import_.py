from __future__ import annotations

import csv

from typing import TYPE_CHECKING

from fetch.domain.types import Doc
from fetch.store.export import site_csv_path


if TYPE_CHECKING:
    from pathlib import Path

    from fetch.domain.types import Row, Site
    from fetch.store.outcomes import OutcomeStore


def import_site(*, store: OutcomeStore, output_csv: Path, site: Site) -> int:
    """Recover successes from a prior per-site export into the store.

    Explicit and opt-in: the store is the durable artifact, so a CSV is never read
    automatically. This is only for reconstructing a lost DB from its exports.
    """
    path = site_csv_path(output_csv, site.name)
    if not path.exists() or path.stat().st_size == 0:
        return 0

    expected = ["doc", *site.columns]
    grouped: dict[str, list[Row]] = {}
    with path.open(newline="", encoding="utf-8") as file_obj:
        reader = csv.reader(file_obj)
        header = next(reader, [])
        if header != expected:
            return 0
        for row in reader:
            if len(row) != len(expected):
                continue
            try:
                doc = str(Doc(row[0]))
            except ValueError:
                continue
            grouped.setdefault(doc, []).append(tuple(row[1:]))

    for doc, rows in grouped.items():
        store.record_import(site=site.name, doc=doc, rows=tuple(rows))
    return len(grouped)
