from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol


if TYPE_CHECKING:
    from collections.abc import Callable

    from browser.result import LookupResult
    from browser.subject import Subject


class SitePage(Protocol):
    """A prepared, single-site page the run loop drives one subject at a time."""

    def lookup(self, subject: str) -> LookupResult: ...


@dataclass(frozen=True)
class BrowserSite:
    # A site value is data plus two callables: open a prepared page on a session,
    # and project a stored column dict into an export row. Adding a site is one
    # sites/<name>/ module plus one registry entry.
    name: str
    url: str
    export_header: tuple[str, ...]
    # Input contract: the sole owner of "can this site answer this subject?". The
    # planner routes a subject only to sites that accept it, so no site is ever
    # handed an identifier it cannot serve.
    accepts: Callable[[Subject], bool]
    open_page: Callable[..., SitePage]
    row: Callable[[str, dict[str, str], str], list[Any]]
