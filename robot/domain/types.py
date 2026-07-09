from __future__ import annotations

import re

from collections import UserString
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
from urllib.parse import urlsplit


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
    NOT_FOUND = "not_found"


# Rows are flat tuples of strings or ints, so the store never has to know any
# site's schema.
Cell = str | int
Row = tuple[Cell, ...]


@dataclass(frozen=True)
class SiteTuning:
    # Lookups a sticky proxy session serves before rotation. A site/protocol
    # constraint, not a proxy knob.
    session_budget: int


@dataclass(frozen=True)
class Endpoint:
    """A distinct HTTP destination a site's ready/lookup depends on.

    Declaring every endpoint a site touches up front lets ready() warm every host a
    lookup actually calls.
    """

    name: str
    url: str
    # False when the endpoint has no standalone GET for warm_endpoints() to call;
    # a site-specific ready() must handle readiness for those endpoints.
    warm: bool = False

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""


@dataclass(frozen=True)
class Site:
    """A lookup target: two functions plus its static data.

    ready warms a fresh proxy-bound client (cookies, block detection); lookup runs
    the request(s) and returns rows aligned to columns, raising the RobotError
    taxonomy on failure. Per-session state rides in the httpx client the pipeline
    hands in.
    """

    name: str
    columns: tuple[str, ...]
    # Input contract: the RUC kinds this site can serve. The planner routes each RUC
    # only to sites whose supports include its kind, so no site is ever handed a RUC
    # it cannot answer.
    supports: frozenset[RucKind]
    # Output contract: a non-empty guarantee. The engine enforces this after every
    # lookup, so a parser's empty fall-through becomes a loud fault.
    allows_empty: bool
    tuning: SiteTuning
    # Every HTTP destination ready/lookup touch, so a second host added to lookup
    # can never silently go unchecked by ready.
    endpoints: tuple[Endpoint, ...]
    ready: Callable[[httpx.AsyncClient, Site], Awaitable[None]]
    lookup: Callable[[httpx.AsyncClient, RUC], Awaitable[tuple[Row, ...]]]


@dataclass(frozen=True)
class Result:
    ruc: RUC
    site: str
    status: Status
    rows: tuple[Row, ...] = ()
    error_code: str = ""
    error_detail: str = ""
    # False when the lane's breaker was open during this attempt; gates whether the
    # attempt counts toward MAX_TOTAL_ATTEMPTS.
    made_healthy_contact: bool = True
    # http_session_id is the per-open session uuid; proxy_id is the sticky proxy
    # label. Do not conflate them when logging or persisting.
    http_session_id: str = ""
    proxy_id: str = ""
    attempt: int = 0


# Mutable so workers can update in place as outcomes land; the run report reflects
# real progress even if a worker raises mid-run.
@dataclass
class RunTotals:
    processed: int = 0
    succeeded: int = 0
    not_found: int = 0
    failed: int = 0
