from __future__ import annotations

import re

from collections import UserString
from enum import Enum


_RUC_RE = re.compile(r"^\d{11}$")
_PHONE_RE = re.compile(r"^9\d{8}$")
_DNI_RE = re.compile(r"^\d{7,8}$")


class SubjectKind(Enum):
    # What is being looked up. A site declares which kinds it serves
    # (BrowserSite.accepts), and the planner routes each subject only to sites that
    # accept its kind, so no site is handed an identifier it cannot answer.
    PHONE = "phone"
    DNI = "dni"
    RUC = "ruc"


class Subject(UserString):
    # The engine's identifier vocabulary, spanning every kind the browser sites take.
    # This package keeps its own copy so it stays independent of fetch's Doc. The store,
    # sites, and driver all speak in it.
    #
    # Classification is by digit shape and must stay unambiguous: Peru mobiles are 9
    # digits leading 9, DNIs are 7-8 digits, RUCs are 11. Lengths never collide, so an
    # 8-digit landline reads as a DNI; 8-digit portability is out of scope and
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
