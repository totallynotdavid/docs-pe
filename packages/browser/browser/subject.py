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
    # A deliberate copy of fetch's Doc, over the kinds browser sites take.
    #
    # Classification is by digit shape, which stays unambiguous only because the
    # lengths never collide: Peru mobiles are 9 digits leading 9, DNIs are 7-8, RUCs
    # are 11. An 8-digit landline therefore reads as a DNI, which is acceptable while
    # portabilidad targets 9-digit mobiles.
    def __init__(self, value: str) -> None:
        normalized = value.strip()
        if _RUC_RE.match(normalized):
            self._kind = SubjectKind.RUC
        elif _PHONE_RE.match(normalized):
            self._kind = SubjectKind.PHONE
        elif _DNI_RE.match(normalized):
            self._kind = SubjectKind.DNI
            # A 7-digit DNI is the modern 8-digit form with a dropped leading zero; pad
            # to the canonical width so the same person keys to one row.
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
