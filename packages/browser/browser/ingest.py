from __future__ import annotations

import csv
import uuid

from dataclasses import dataclass
from typing import TYPE_CHECKING

from browser.ruc import RUC


if TYPE_CHECKING:
    from pathlib import Path


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


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
