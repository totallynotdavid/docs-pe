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
    # True when the failing attempts hit a healthy provider (breaker closed); the
    # lane sets it False when the breaker was open. Gates MAX_TOTAL_ATTEMPTS.
    made_healthy_contact: bool = True
    # http_session_id is the per-open OSIPTEL session uuid; proxy_id is the sticky
    # proxy label. Different identifiers, both persisted, so keep the names honest.
    http_session_id: str = ""
    proxy_id: str = ""
    attempt: int = 0


# Mutable accumulator the lanes update in place as outcomes land, so the run
# report reflects real progress even if a lane raises mid-run.
@dataclass
class RunTotals:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0


@dataclass(frozen=True)
class RunReport:
    rows_read: int
    valid: int
    ignored: int
    duplicates: int
    already_done: int
    seeded: int
    pending: int
    processed: int
    succeeded: int
    failed: int
    remaining: int
    total_succeeded: int
    # Durable, cap-aware split: terminal_failed have left the work set for good;
    # retryable are transient failures still eligible for re-fetch next run.
    total_terminal_failed: int
    total_retryable: int
