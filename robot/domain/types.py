from __future__ import annotations

import re

from collections import UserString
from dataclasses import dataclass
from enum import Enum


_RUC_RE = re.compile(r"^\d{11}$")


class RUC(UserString):
    def __init__(self, value: str) -> None:
        normalized = value.strip()
        if not _RUC_RE.match(normalized):
            msg = f"invalid RUC {value!r}: must be 11 digits"
            raise ValueError(msg)
        super().__init__(normalized)


class Status(str, Enum):
    OK = "ok"
    FAILED = "failed"


@dataclass(frozen=True)
class CarrierCount:
    carrier: str
    lines: int


@dataclass
class LookupResult:
    ruc: RUC
    status: Status
    total_lines: int = 0
    carrier_counts: tuple[CarrierCount, ...] = ()
    error_code: str = ""
    error_detail: str = ""
    session_id: str = ""
    proxy_id: str = ""


@dataclass
class WorkerSummary:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0


@dataclass
class RunSummary:
    rows_read: int = 0
    valid: int = 0
    ignored: int = 0
    duplicates: int = 0
    skipped: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
