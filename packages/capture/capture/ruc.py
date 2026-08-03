from __future__ import annotations

import re

from collections import UserString


_RUC_RE = re.compile(r"^\d{11}$")


class RUC(UserString):
    # A deliberate copy: validating an 11-digit id is not worth a cross-package
    # dependency. No lookup here routes by kind, so there is no kind field.
    def __init__(self, value: str) -> None:
        normalized = value.strip()
        if not _RUC_RE.match(normalized):
            msg = f"invalid RUC {value!r}: must be 11 digits"
            raise ValueError(msg)
        super().__init__(normalized)
