from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from capture.result import LookupResult


@dataclass(frozen=True)
class CaptureSite:
    # A site value is data plus two callables: parse an in-page result payload
    # into a stored column dict, and project that column dict into an export row.
    # `script` is the in-page recipe pasted into a reputable Chrome. `origin` is
    # the only host the relay accepts requests from. Adding a site is one new
    # sites/<name>/ module plus one registry entry.
    name: str
    origin: str
    export_header: tuple[str, ...]
    script: Path
    parse: Callable[..., LookupResult]
    row: Callable[[str, dict[str, str], str], list[Any]]
