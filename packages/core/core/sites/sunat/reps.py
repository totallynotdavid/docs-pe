from __future__ import annotations

import re

from dataclasses import dataclass

from core.domain.errors import ProviderSchemaError
from core.sites.sunat.parser import clean, ensure_no_error_page


@dataclass(frozen=True)
class RepRecord:
    doc_type: str
    num_doc: str
    nombre: str
    cargo: str
    fecha_desde: str


# SUNAT keeps representative records in <tbody>; <thead> is not data.
_TBODY_RE = re.compile(r"<tbody>(?P<body>.*?)</tbody>", re.IGNORECASE | re.DOTALL)
_ROW_RE = re.compile(r"<tr>(?P<row>.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<td[^>]*>(?P<cell>.*?)</td>", re.IGNORECASE | re.DOTALL)
_REP_CELL_COUNT = 5
_NO_REPS_MARKER = "No se encontro información para representantes legales"


def build_reps_body(*, ruc: str) -> dict[str, str]:
    return {
        "accion": "getRepLeg",
        "contexto": "ti-it",
        "modo": "1",
        "desRuc": "",  # echoed back unvalidated; required key, so send it empty
        "nroRuc": ruc,
    }


def parse_reps(page: str) -> tuple[RepRecord, ...]:
    if _NO_REPS_MARKER in page:
        return ()
    ensure_no_error_page(page)
    body = _TBODY_RE.search(page)
    if body is None:
        msg = "sunat reps response has no results table"
        raise ProviderSchemaError(msg)

    reps: list[RepRecord] = []
    for row in _ROW_RE.finditer(body.group("body")):
        cells = [
            clean(cell.group("cell")) for cell in _CELL_RE.finditer(row.group("row"))
        ]
        if len(cells) != _REP_CELL_COUNT:
            continue
        reps.append(
            RepRecord(
                doc_type=cells[0],
                num_doc=cells[1],
                nombre=cells[2],
                cargo=cells[3],
                fecha_desde=cells[4],
            )
        )
    return tuple(reps)
