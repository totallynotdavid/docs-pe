from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LookupResult:
    subject: str
    columns: dict[str, str]
    elapsed_ms: int
    mint_ms: int
