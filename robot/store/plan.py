from __future__ import annotations

import csv

from dataclasses import dataclass
from typing import TYPE_CHECKING

from robot.domain.types import RUC


if TYPE_CHECKING:
    from pathlib import Path

    from robot.domain.types import Site


@dataclass(frozen=True)
class PlanCounts:
    rows_read: int
    valid: int
    ignored: int
    duplicates: int


def read_rucs(input_csv: Path, *, dedupe: bool) -> tuple[list[RUC], PlanCounts]:
    rows_read = valid = ignored = duplicates = 0
    seen: set[str] = set()
    rucs: list[RUC] = []

    with input_csv.open(newline="", encoding="utf-8-sig") as file_obj:
        for row in csv.reader(file_obj):
            rows_read += 1
            if not row or not row[0].strip():
                ignored += 1
                continue
            try:
                ruc = RUC(row[0])
            except ValueError:
                ignored += 1
                continue

            normalized = str(ruc)
            if dedupe and normalized in seen:
                duplicates += 1
                continue
            seen.add(normalized)
            valid += 1
            rucs.append(ruc)

    return rucs, PlanCounts(
        rows_read=rows_read, valid=valid, ignored=ignored, duplicates=duplicates
    )


def _serves(site: Site, ruc: RUC) -> bool:
    # The sole owner of "can this site answer this RUC?"; both routing helpers below
    # ask through here so the predicate never drifts between them.
    return ruc.kind in site.supports


def plan_pending(
    rucs: list[RUC], sites: list[Site], done_pairs: set[tuple[str, str]]
) -> dict[str, list[RUC]]:
    # Excludes pairs already resolved in the store, so a resumed run never
    # redoes completed work.
    return {
        site.name: [
            ruc
            for ruc in rucs
            if _serves(site, ruc) and (site.name, str(ruc)) not in done_pairs
        ]
        for site in sites
    }


def count_unrouted(rucs: list[RUC], sites: list[Site]) -> int:
    # A RUC no selected site can serve falls out of every output; count it so the gap
    # is visible, not a silent absence.
    return sum(1 for ruc in rucs if not any(_serves(site, ruc) for site in sites))
