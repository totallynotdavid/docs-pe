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
    DNI = "dni"
    RUC = "ruc"


class RucKind(Enum):
    NATURAL = "natural"
    JURIDICA = "juridica"


class Doc(UserString):
    def __init__(self, value: str) -> None:
        normalized = value.strip()

        if _RUC_RE.match(normalized):
            self._kind = DocKind.RUC
        elif _DNI_RE.match(normalized):
            self._kind = DocKind.DNI

            # Seven-digit DNIs omit the canonical leading zero.
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


Cell = str | int
Row = tuple[Cell, ...]


@dataclass(frozen=True)
class SiteTuning:
    session_budget: int


@dataclass(frozen=True)
class Endpoint:
    """A named HTTP destination used by a site."""

    name: str
    url: str

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""


@dataclass(frozen=True)
class Projection:
    """A derived CSV view over a site's stored rows."""

    name: str
    columns: tuple[str, ...]
    project: Callable[[tuple[Row, ...]], tuple[Row, ...]]


@dataclass(frozen=True)
class Site:
    """A lookup target and its static configuration."""

    name: str
    columns: tuple[str, ...]
    accepts: Callable[[Doc], bool]

    # False turns an empty parser result into an error.
    allows_empty: bool

    tuning: SiteTuning
    endpoints: tuple[Endpoint, ...]
    ready: Callable[[httpx.AsyncClient, Site], Awaitable[None]]
    lookup: Callable[[httpx.AsyncClient, Doc], Awaitable[tuple[Row, ...]]]
    projections: tuple[Projection, ...] = ()
    stable: bool = False


@dataclass(frozen=True)
class Result:
    doc: Doc
    site: str
    status: Status
    rows: tuple[Row, ...] = ()
    error_code: str = ""
    error_detail: str = ""

    # False when the circuit breaker prevented a real provider attempt.
    made_healthy_contact: bool = True

    # The HTTP session UUID and sticky proxy identifier are different values.
    http_session_id: str = ""
    proxy_id: str = ""

    attempt: int = 0


# Workers update this in place so partial progress survives a failed run.
@dataclass
class RunTotals:
    processed: int = 0
    succeeded: int = 0
    not_found: int = 0
    failed: int = 0
