from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol


if TYPE_CHECKING:
    from collections.abc import Callable

    from browser.result import LookupResult


class SitePage(Protocol):
    """A prepared, single-site page the run loop drives one RUC at a time."""

    def lookup(self, ruc: str) -> LookupResult: ...


@dataclass(frozen=True)
class BrowserSite:
    # A site value is data plus two callables: open a prepared page on a
    # controller, and project a stored column dict into an export row. Adding a
    # site is one sites/<name>/ module plus one registry entry.
    name: str
    url: str
    export_header: tuple[str, ...]
    open_page: Callable[..., SitePage]
    row: Callable[[str, dict[str, str], str], list[Any]]
