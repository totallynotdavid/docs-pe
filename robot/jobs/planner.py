from __future__ import annotations

import csv

from dataclasses import dataclass
from typing import TYPE_CHECKING

from robot.domain.types import RUC


if TYPE_CHECKING:
    from pathlib import Path

    from robot.jobs.store import JobStore


@dataclass
class PlanSummary:
    rows_read: int = 0
    valid: int = 0
    ignored: int = 0
    duplicates: int = 0
    skipped: int = 0
    inserted: int = 0


def plan_jobs(
    *,
    input_csv: Path,
    store: JobStore,
    dedupe: bool,
) -> PlanSummary:
    summary = PlanSummary()
    seen: set[str] = set()

    with input_csv.open(newline="", encoding="utf-8-sig") as file_obj:
        for row in csv.reader(file_obj):
            summary.rows_read += 1
            if not row or not row[0].strip():
                summary.ignored += 1
                continue

            try:
                ruc = RUC(row[0])
            except ValueError:
                summary.ignored += 1
                continue

            normalized = str(ruc)
            if dedupe and normalized in seen:
                summary.duplicates += 1
                continue
            seen.add(normalized)
            summary.valid += 1

            if store.insert_pending(ruc):
                summary.inserted += 1
            else:
                summary.skipped += 1

    return summary
