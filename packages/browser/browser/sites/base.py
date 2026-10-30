from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol


if TYPE_CHECKING:
    from collections.abc import Callable

    from browser.result import LookupResult
    from browser.subject import Subject


class SitePage(Protocol):
    def lookup(self, subject: str) -> LookupResult: ...


@dataclass(frozen=True)
class BrowserSite:
    name: str
    url: str
    export_header: tuple[str, ...]

    accepts: Callable[[Subject], bool]

    open_page: Callable[..., SitePage]
    # A subject can project to zero or more CSV rows.
    rows: Callable[[str, dict[str, str], str], list[list[Any]]]
