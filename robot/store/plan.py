from __future__ import annotations

import csv

from dataclasses import dataclass
from typing import TYPE_CHECKING

from robot.domain.types import RUC


if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class PlanReport:
    rows_read: int
    valid: int
    ignored: int
    duplicates: int
    already_done: int
    pending: int


def derive_pending(
    *,
    input_csv: Path,
    done: set[str],
    dedupe: bool,
) -> tuple[list[RUC], PlanReport]:
    rows_read = valid = ignored = duplicates = already_done = 0
    seen: set[str] = set()
    pending: list[RUC] = []

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

            if normalized in done:
                already_done += 1
                continue
            pending.append(ruc)

    report = PlanReport(
        rows_read=rows_read,
        valid=valid,
        ignored=ignored,
        duplicates=duplicates,
        already_done=already_done,
        pending=len(pending),
    )
    return pending, report
