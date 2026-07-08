from __future__ import annotations

import html as html_lib
import re

from dataclasses import dataclass

from robot.domain.errors import ProviderSchemaError


@dataclass(frozen=True)
class SunatRecord:
    tipo_doc: str
    num_doc: str
    nombre: str


@dataclass(frozen=True)
class RepRecord:
    doc_type: str
    num_doc: str
    nombre: str
    cargo: str
    fecha_desde: str


# The value block: <h4>Tipo de Documento:</h4> ... <p ...>DNI  19187661  - NAME</p>
_TIPO_DOC_RE = re.compile(
    r"Tipo de Documento:\s*</h4>.*?<p[^>]*>(?P<value>.*?)</p>",
    re.IGNORECASE | re.DOTALL,
)
# The ficha RUC header: <h4>...mero de RUC:</h4> ... <h4>20100000335 - RAZON SOCIAL</h4>.
# Anchored on the tail of "Numero" to sidestep the &uacute; entity.
_RAZON_SOCIAL_RE = re.compile(
    r"mero de RUC:\s*</h4>.*?<h4[^>]*>(?P<value>[^<]*)</h4>",
    re.IGNORECASE | re.DOTALL,
)
# The legal-representatives table body; header row lives in a separate <thead>.
_TBODY_RE = re.compile(r"<tbody>(?P<body>.*?)</tbody>", re.IGNORECASE | re.DOTALL)
_ROW_RE = re.compile(r"<tr>(?P<row>.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<td[^>]*>(?P<cell>.*?)</td>", re.IGNORECASE | re.DOTALL)
_REP_CELL_COUNT = 5

_RESULT_MARKER = "Resultado de la B"
_ERROR_MARKERS = ("Pagina de Error", "Surgieron problemas")
_NO_REPS_MARKER = "No se encontro información para representantes legales"


def parse_tipo_documento(page: str) -> SunatRecord | None:
    # None means a well-formed page carries no document row.
    _ensure_no_error_page(page)
    if _RESULT_MARKER not in page:
        msg = "sunat response is not a result page"
        raise ProviderSchemaError(msg)

    match = _TIPO_DOC_RE.search(page)
    if match is None:
        return None
    text = _clean(match.group("value"))
    if not text or text == "-":
        return None

    doc_part, _, nombre = text.partition(" - ")
    tokens = doc_part.split()
    if not tokens:
        return None
    # The document number is the trailing token; the type is everything before it,
    # so multi-word types (Carnet de Extranjeria) survive.
    if len(tokens) == 1:
        return SunatRecord(tipo_doc=tokens[0], num_doc="", nombre=nombre.strip())
    return SunatRecord(
        tipo_doc=" ".join(tokens[:-1]),
        num_doc=tokens[-1],
        nombre=nombre.strip(),
    )


def parse_razon_social(page: str) -> str:
    # A juridica ficha RUC always carries its razon social in the header; its
    # absence on a non-error page is schema drift, not an empty success.
    _ensure_no_error_page(page)
    match = _RAZON_SOCIAL_RE.search(page)
    if match is None:
        msg = "sunat consulta is missing the razon social"
        raise ProviderSchemaError(msg)
    _, sep, nombre = _clean(match.group("value")).partition(" - ")
    if not sep or not nombre:
        msg = "sunat consulta razon social is malformed"
        raise ProviderSchemaError(msg)
    return nombre


def parse_reps(page: str) -> tuple[RepRecord, ...]:
    if _NO_REPS_MARKER in page:
        return ()
    _ensure_no_error_page(page)
    body = _TBODY_RE.search(page)
    if body is None:
        msg = "sunat reps response has no results table"
        raise ProviderSchemaError(msg)

    reps: list[RepRecord] = []
    for row in _ROW_RE.finditer(body.group("body")):
        cells = [
            _clean(cell.group("cell")) for cell in _CELL_RE.finditer(row.group("row"))
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


def _ensure_no_error_page(page: str) -> None:
    if any(marker in page for marker in _ERROR_MARKERS):
        msg = "sunat returned an error page"
        raise ProviderSchemaError(msg)


def _clean(value: str) -> str:
    return " ".join(html_lib.unescape(value).split())
