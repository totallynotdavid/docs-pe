from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LookupResult:
    ruc: str
    # Site-defined values are already stringified for storage and CSV export.
    columns: dict[str, str]
    elapsed_ms: int
    mint_ms: int
