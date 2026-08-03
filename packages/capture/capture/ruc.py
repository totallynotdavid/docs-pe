from __future__ import annotations

import re

from collections import UserString


_RUC_RE = re.compile(r"\d{11}")


class RUC(UserString):
    def __init__(self, value: str) -> None:
        normalized = value.strip()
        if not _RUC_RE.fullmatch(normalized):
            msg = f"invalid RUC {value!r}: must be 11 digits"
            raise ValueError(msg)
        super().__init__(normalized)
