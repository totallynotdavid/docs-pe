from __future__ import annotations

import re

from collections import UserString


_RUC_RE = re.compile(r"^\d{11}$")


class RUC(UserString):
    # This package keeps its own RUC copy rather than depend on another package
    # just to validate an 11-digit id. No lookup here routes by kind, so there is
    # no kind field; add it only if one ever needs it.
    def __init__(self, value: str) -> None:
        normalized = value.strip()
        if not _RUC_RE.match(normalized):
            msg = f"invalid RUC {value!r}: must be 11 digits"
            raise ValueError(msg)
        super().__init__(normalized)
