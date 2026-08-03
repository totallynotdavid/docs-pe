from __future__ import annotations

import csv

from dataclasses import dataclass
from typing import TYPE_CHECKING

from fetch.domain.types import Doc


if TYPE_CHECKING:
    from pathlib import Path

    from fetch.domain.types import Site


@dataclass(frozen=True)
class PlanCounts:
    rows_read: int
    valid: int
    ignored: int
    duplicates: int


def read_docs(input_csv: Path, *, dedupe: bool) -> tuple[list[Doc], PlanCounts]:
    rows_read = valid = ignored = duplicates = 0
    seen: set[str] = set()
    docs: list[Doc] = []

    with input_csv.open(newline="", encoding="utf-8-sig") as file_obj:
        for row in csv.reader(file_obj):
            rows_read += 1
            if not row or not row[0].strip():
                ignored += 1
                continue
            try:
                doc = Doc(row[0])
            except ValueError:
                ignored += 1
                continue

            normalized = str(doc)
            if dedupe and normalized in seen:
                duplicates += 1
                continue
            seen.add(normalized)
            valid += 1
            docs.append(doc)

    return docs, PlanCounts(
        rows_read=rows_read, valid=valid, ignored=ignored, duplicates=duplicates
    )


def _serves(site: Site, doc: Doc) -> bool:
    return site.accepts(doc)


def plan_pending(
    docs: list[Doc], sites: list[Site], done_pairs: set[tuple[str, str]]
) -> dict[str, list[Doc]]:
    # Excludes pairs already resolved, so a resumed run never redoes them.
    return {
        site.name: [
            doc
            for doc in docs
            if _serves(site, doc) and (site.name, str(doc)) not in done_pairs
        ]
        for site in sites
    }


def count_unrouted(docs: list[Doc], sites: list[Site]) -> int:
    # A document no selected site can serve falls out of every output; count it so the
    # gap is visible.
    return sum(1 for doc in docs if not any(_serves(site, doc) for site in sites))
