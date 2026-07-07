from __future__ import annotations

import re

from collections import UserString
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import httpx


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


# One output cell and a row of them, aligned to a site's columns. Every site
# flattens its typed record into this generic shape for storage and export, so the
# store never has to know any site's schema.
Cell = str | int
Row = tuple[Cell, ...]


@dataclass(frozen=True)
class SiteTuning:
    # session_budget: lookups a sticky proxy session serves before rotation. It is a
    # site/protocol constraint (OSIPTEL must rotate every lookup), not a proxy knob.
    session_budget: int


@dataclass(frozen=True)
class Site:
    """A lookup target: two functions plus its static data, no behavior or state.

    ready warms a fresh proxy-bound client (cookies, block detection); lookup runs
    the request(s) and returns rows aligned to columns, raising the RobotError
    taxonomy on failure. Any per-session state rides in the httpx client the
    pipeline hands in, so nothing needs to be modeled here.
    """

    name: str
    columns: tuple[str, ...]
    tuning: SiteTuning
    ready: Callable[[httpx.AsyncClient], Awaitable[None]]
    lookup: Callable[[httpx.AsyncClient, RUC], Awaitable[tuple[Row, ...]]]


@dataclass(frozen=True)
class Result:
    ruc: RUC
    site: str
    status: Status
    rows: tuple[Row, ...] = ()
    error_code: str = ""
    error_detail: str = ""
    # True unless the lane's breaker was open during this attempt (see
    # domain/policy.py:MAX_TOTAL_ATTEMPTS for what this gates).
    made_healthy_contact: bool = True
    # http_session_id is the per-open session uuid; proxy_id is the sticky proxy
    # label. Different identifiers; do not conflate them when logging or persisting.
    http_session_id: str = ""
    proxy_id: str = ""
    attempt: int = 0


# Mutable per-site accumulator the workers update in place as outcomes land, so the
# run report reflects real progress even if a worker raises mid-run.
@dataclass
class RunTotals:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
