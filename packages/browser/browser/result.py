from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LookupResult:
    # The site-neutral value the store speaks. columns are already stringified so
    # the store and CSV export never need to know a site's field types.
    subject: str
    columns: dict[str, str]
    elapsed_ms: int
    mint_ms: int
