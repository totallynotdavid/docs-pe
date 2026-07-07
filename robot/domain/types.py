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


class RucKind(Enum):
    NATURAL = "natural"
    JURIDICA = "juridica"


class RUC(UserString):
    def __init__(self, value: str) -> None:
        normalized = value.strip()
        if not _RUC_RE.match(normalized):
            msg = f"invalid RUC {value!r}: must be 11 digits"
            raise ValueError(msg)
        super().__init__(normalized)

    @property
    def kind(self) -> RucKind:
        return RucKind.JURIDICA if self.data.startswith("20") else RucKind.NATURAL


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
    # Lookups a sticky proxy session serves before rotation. A site/protocol
    # constraint, not a proxy knob.
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
    # Input contract: the RUC kinds this site can serve. The planner routes each RUC
    # only to sites whose supports include its kind, so a mixed input fans out by
    # taxpayer type (RUC-10 to a natural-person lookup, RUC-20 to a company lookup)
    # with no site ever handed a RUC it cannot answer.
    supports: frozenset[RucKind]
    # Output contract: may a lookup legitimately return zero rows? The engine enforces
    # this after every lookup (pipeline/fetch.py), so a site whose result is always
    # non-empty (a persona always has a document, a company always has a rep) turns an
    # empty result into a loud fault instead of a silent blank success. Emptiness is a
    # site policy declared here, never a parser's fall-through.
    allows_empty: bool
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
