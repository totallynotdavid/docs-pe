from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from capture.result import LookupResult


@dataclass(frozen=True)
class CaptureSite:
    name: str
    origin: str
    export_header: tuple[str, ...]
    script: Path
    parse: Callable[..., LookupResult]
    row: Callable[[str, dict[str, str], str], list[object]]
