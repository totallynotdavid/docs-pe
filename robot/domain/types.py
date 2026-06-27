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


@dataclass(frozen=True)
class LookupResult:
    ruc: RUC
    status: Status
    total_lines: int = 0
    carrier_counts: tuple[CarrierCount, ...] = ()
    error_code: str = ""
    error_detail: str = ""
    # http_session_id is the per-open OSIPTEL session uuid; proxy_id is the
    # sticky proxy label the provider assigns. They are different identifiers
    # and the store persists both, so keep the names honest about which is which.
    http_session_id: str = ""
    proxy_id: str = ""
    attempt: int = 0


@dataclass
class LaneTotals:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0


@dataclass(frozen=True)
class RunReport:
    rows_read: int
    valid: int
    ignored: int
    duplicates: int
    skipped: int
    inserted: int
    seeded: int
    processed: int
    succeeded: int
    failed: int
    pending: int
    in_progress: int
    failed_jobs: int
