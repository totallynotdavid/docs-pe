from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from capture.result import LookupResult


@dataclass(frozen=True)
class CaptureSite:
    name: str
    # The only host the relay accepts requests from.
    origin: str
    export_header: tuple[str, ...]
    # The in-page recipe a person pastes into their own Chrome.
    script: Path
    parse: Callable[..., LookupResult]
    row: Callable[[str, dict[str, str], str], list[Any]]
