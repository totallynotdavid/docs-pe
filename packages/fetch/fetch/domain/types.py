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


_DNI_RE = re.compile(r"^\d{7,8}$")
_RUC_RE = re.compile(r"^\d{11}$")


class DocKind(Enum):
    # The document type. Sets the valid length and OSIPTEL's IdTipoDoc (DNI=1, RUC=2).
    DNI = "dni"
    RUC = "ruc"


class RucKind(Enum):
    # A RUC's subtype, read off the leading digits. Only RUC docs have one; it is the
    # routing key that splits natural persons (sunat) from entities (sunat_reps).
    NATURAL = "natural"
    JURIDICA = "juridica"


class Doc(UserString):
    # The identifier vocabulary Site, Result, and the planner all speak in.
    def __init__(self, value: str) -> None:
        normalized = value.strip()
        if _RUC_RE.match(normalized):
            self._kind = DocKind.RUC
        elif _DNI_RE.match(normalized):
            self._kind = DocKind.DNI
            # A 7-digit DNI is the modern 8-digit form with a dropped leading zero, so
            # pad to the canonical width; the same person keys to one row.
            normalized = normalized.zfill(8)
        else:
            msg = (
                f"invalid document {value!r}: expected a 7-8 digit DNI or 11-digit RUC"
            )
            raise ValueError(msg)
        super().__init__(normalized)

    @property
    def kind(self) -> DocKind:
        return self._kind

    @property
    def ruc_kind(self) -> RucKind | None:
        if self._kind is not DocKind.RUC:
            return None
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
    # Lookups a sticky proxy session serves before rotation. Set by what the site's
    # protocol requires, not by the proxy vendor.
    session_budget: int


@dataclass(frozen=True)
class Endpoint:
    """A named HTTP destination a site calls: name for diagnostics, url to hit."""

    name: str
    url: str

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""


@dataclass(frozen=True)
class Projection:
    """A named alternate view over a site's stored rows, materialized on export.

    The store holds each site's richest form once, so a derived CSV costs a pure
    function over those rows and no second crawl.
    """

    name: str
    columns: tuple[str, ...]
    project: Callable[[tuple[Row, ...]], tuple[Row, ...]]


@dataclass(frozen=True)
class Site:
    """A lookup target: two functions plus its static data.

    ready warms a fresh proxy-bound client (cookies, block detection); lookup runs
    the request(s) and returns rows aligned to columns, raising the FetchError
    taxonomy on failure. Per-session state rides in the httpx client the pipeline
    hands in.
    """

    name: str
    columns: tuple[str, ...]
    # The sole owner of "can this site answer this document?". The planner routes on it.
    accepts: Callable[[Doc], bool]
    # False makes the engine fault on an empty result, so a parser's empty
    # fall-through is loud.
    allows_empty: bool
    tuning: SiteTuning
    # The hosts ready() warms before the first lookup.
    endpoints: tuple[Endpoint, ...]
    ready: Callable[[httpx.AsyncClient, Site], Awaitable[None]]
    lookup: Callable[[httpx.AsyncClient, Doc], Awaitable[tuple[Row, ...]]]
    # Derived CSV views exported alongside the canonical rows, no extra fetch.
    projections: tuple[Projection, ...] = ()
    # Whether the portal may offer this site to a team.
    stable: bool = False


@dataclass(frozen=True)
class Result:
    doc: Doc
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
