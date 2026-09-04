from __future__ import annotations

import csv

from typing import TYPE_CHECKING

from core.domain.types import Doc

from cli.store.export import site_csv_path


if TYPE_CHECKING:
    from pathlib import Path

    from core.domain.types import Row, Site

    from cli.store.outcomes import OutcomeStore


def import_site(*, store: OutcomeStore, output_csv: Path, site: Site) -> int:
    """Restore successful outcomes from a prior site export."""

    path = site_csv_path(output_csv, site.name)

    if not path.exists() or path.stat().st_size == 0:
        return 0

    expected = ["doc", *site.columns]
    grouped: dict[str, list[Row]] = {}

    with path.open(newline="", encoding="utf-8") as file:
        reader = csv.reader(file)
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
        store.record_import(
            site=site.name,
            doc=doc,
            rows=tuple(rows),
        )

    return len(grouped)
