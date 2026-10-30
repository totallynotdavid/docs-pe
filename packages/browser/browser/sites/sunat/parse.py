from __future__ import annotations

import html as html_lib
import re

from dataclasses import dataclass

from browser.errors import BrowserError


@dataclass(frozen=True)
class Identity:
    tipo_doc: str
    num_doc: str
    nombre: str
    tipo_contribuyente: str


@dataclass(frozen=True)
class Representative:
    doc_type: str
    num_doc: str
    nombre: str
    cargo: str
    fecha_desde: str


_TIPO_DOC_RE = re.compile(
    r"Tipo de Documento:\s*</h4>.*?<p[^>]*>(?P<value>.*?)</p>",
    re.IGNORECASE | re.DOTALL,
)

# A sucesión indivisa has no document row, so its name must come from the RUC row.
_RUC_ROW_RE = re.compile(
    r"mero de RUC:\s*</h4>.*?<h4[^>]*>(?P<value>.*?)</h4>",
    re.IGNORECASE | re.DOTALL,
)

_TIPO_CONTRIBUYENTE_RE = re.compile(
    r"Tipo Contribuyente:\s*</h4>.*?<p[^>]*>(?P<value>.*?)</p>",
    re.IGNORECASE | re.DOTALL,
)

_SUCESION_RE = re.compile(r"SUCESI[ÓO]N\s+INDIVISA", re.IGNORECASE)
_SUCESION_PREFIX_RE = re.compile(r"^SUCESI[ÓO]N\s+INDIVISA\s+", re.IGNORECASE)

_RESULT_MARKER = "Resultado de la B"
_ERROR_MARKERS = ("Pagina de Error", "Surgieron problemas")

# SUNAT returns an ordinary result page for an unregistered RUC.
_NOT_REGISTERED_RE = re.compile(r"RUC\s+\d+\s+consultado no es v", re.IGNORECASE)

# SUNAT puts representative records in <tbody>. The <thead> rows are headers.
_TBODY_RE = re.compile(r"<tbody>(?P<body>.*?)</tbody>", re.IGNORECASE | re.DOTALL)
_ROW_RE = re.compile(r"<tr>(?P<row>.*?)</tr>", re.IGNORECASE | re.DOTALL)
_CELL_RE = re.compile(r"<td[^>]*>(?P<cell>.*?)</td>", re.IGNORECASE | re.DOTALL)
_REP_CELL_COUNT = 5
_NO_REPS_MARKER = "No se encontro información para representantes legales"


def parse_identity(page: str) -> Identity | None:
    """Return a natural person's identity, or None when SUNAT has no such RUC."""
    if not _is_registered(page):
        return None

    tipo_contribuyente = _field(_TIPO_CONTRIBUYENTE_RE, page)
    tipo_doc, num_doc, nombre = _document_row(page)

    if not nombre:
        # Only a sucesión indivisa may omit the document row.
        if not _SUCESION_RE.search(tipo_contribuyente):
            msg = "sunat result page has no document row"
            raise BrowserError(msg)

        nombre = _SUCESION_PREFIX_RE.sub("", _name_from_ruc_row(page)).strip()

        if not nombre:
            msg = "sunat result page has no name"
            raise BrowserError(msg)

    return Identity(
        tipo_doc=tipo_doc,
        num_doc=num_doc,
        nombre=nombre,
        tipo_contribuyente=tipo_contribuyente,
    )


def parse_razon_social(page: str) -> str | None:
    """Return a legal entity's name, or None when SUNAT has no such RUC."""
    if not _is_registered(page):
        return None

    razon_social = _name_from_ruc_row(page)

    if not razon_social:
        msg = "sunat result page has no razon social"
        raise BrowserError(msg)

    return razon_social


def parse_representatives(page: str) -> tuple[Representative, ...]:
    if _NO_REPS_MARKER in page:
        return ()

    _ensure_no_error_page(page)
    body = _TBODY_RE.search(page)

    if body is None:
        msg = "sunat reps response has no results table"
        raise BrowserError(msg)

    reps: list[Representative] = []

    for row in _ROW_RE.finditer(body.group("body")):
        cells = [
            _clean(cell.group("cell")) for cell in _CELL_RE.finditer(row.group("row"))
        ]

        if len(cells) != _REP_CELL_COUNT:
            continue

        reps.append(Representative(*cells))

    return tuple(reps)


def _is_registered(page: str) -> bool:
    _ensure_no_error_page(page)

    if _NOT_REGISTERED_RE.search(page):
        return False

    if _RESULT_MARKER not in page:
        msg = "sunat response is not a result page"
        raise BrowserError(msg)

    return True


def _document_row(page: str) -> tuple[str, str, str]:
    match = _TIPO_DOC_RE.search(page)

    if match is None:
        return "", "", ""

    text = _clean(match.group("value"))

    if not text or text == "-":
        return "", "", ""

    doc_part, _, nombre = text.partition(" - ")
    tokens = doc_part.split()

    if not tokens:
        return "", "", ""

    if len(tokens) == 1:
        return tokens[0], "", nombre.strip()

    return " ".join(tokens[:-1]), tokens[-1], nombre.strip()


def _name_from_ruc_row(page: str) -> str:
    match = _RUC_ROW_RE.search(page)

    if match is None:
        return ""

    _, _, nombre = _clean(match.group("value")).partition(" - ")

    return nombre.strip()


def _field(pattern: re.Pattern[str], page: str) -> str:
    match = pattern.search(page)

    return _clean(match.group("value")) if match is not None else ""


def _ensure_no_error_page(page: str) -> None:
    if any(marker in page for marker in _ERROR_MARKERS):
        msg = "sunat returned an error page"
        raise BrowserError(msg)


def _clean(value: str) -> str:
    return " ".join(html_lib.unescape(value).split())
