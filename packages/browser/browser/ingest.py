from __future__ import annotations

import csv
import uuid

from dataclasses import dataclass
from typing import TYPE_CHECKING

from browser.subject import Subject


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


def read_subjects(input_csv: Path, *, dedupe: bool) -> tuple[list[Subject], PlanCounts]:
    rows_read = valid = ignored = duplicates = 0
    seen: set[str] = set()
    subjects: list[Subject] = []

    with input_csv.open(newline="", encoding="utf-8-sig") as file_obj:
        for row in csv.reader(file_obj):
            rows_read += 1
            if not row or not row[0].strip():
                ignored += 1
                continue
            try:
                subject = Subject(row[0])
            except ValueError:
                ignored += 1
                continue

            normalized = str(subject)
            if dedupe and normalized in seen:
                duplicates += 1
                continue
            seen.add(normalized)
            valid += 1
            subjects.append(subject)

    return subjects, PlanCounts(
        rows_read=rows_read, valid=valid, ignored=ignored, duplicates=duplicates
    )
