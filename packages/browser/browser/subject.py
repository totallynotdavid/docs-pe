from __future__ import annotations

import re

from collections import UserString
from enum import Enum


_RUC_RE = re.compile(r"^\d{11}$")
_PHONE_RE = re.compile(r"^9\d{8}$")
_DNI_RE = re.compile(r"^\d{7,8}$")


class SubjectKind(Enum):
    PHONE = "phone"
    DNI = "dni"
    RUC = "ruc"


class Subject(UserString):
    def __init__(self, value: str) -> None:
        normalized = value.strip()

        if _RUC_RE.match(normalized):
            self._kind = SubjectKind.RUC
        elif _PHONE_RE.match(normalized):
            self._kind = SubjectKind.PHONE
        elif _DNI_RE.match(normalized):
            self._kind = SubjectKind.DNI
            # Canonicalize legacy 7-digit DNIs to 8 digits.
            normalized = normalized.zfill(8)
        else:
            msg = (
                f"invalid subject {value!r}: expected a 9-digit phone, "
                "7-8 digit DNI, or 11-digit RUC"
            )
            raise ValueError(msg)

        super().__init__(normalized)

    @property
    def kind(self) -> SubjectKind:
        return self._kind
