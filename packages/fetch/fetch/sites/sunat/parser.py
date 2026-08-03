from __future__ import annotations

import html as html_lib
import re

from dataclasses import dataclass

from fetch.domain.errors import ProviderSchemaError, RucNotFoundError


@dataclass(frozen=True)
class SunatRecord:
    tipo_doc: str
    num_doc: str
    nombre: str
    tipo_contribuyente: str


# The value block: <h4>Tipo de Documento:</h4> ... <p ...>DNI  19187661  - NAME</p>
_TIPO_DOC_RE = re.compile(
    r"Tipo de Documento:\s*</h4>.*?<p[^>]*>(?P<value>.*?)</p>",
    re.IGNORECASE | re.DOTALL,
)
# The RUC row reads "<ruc> - <name>" and is the only place the name is always
# available: a sucesion indivisa has no document row to read it from. Matched on
# the tail of the label because the page writes it "N&uacute;mero de RUC:".
_RUC_ROW_RE = re.compile(
    r"mero de RUC:\s*</h4>.*?<h4[^>]*>(?P<value>.*?)</h4>",
    re.IGNORECASE | re.DOTALL,
)
_TIPO_CONTRIBUYENTE_RE = re.compile(
    r"Tipo Contribuyente:\s*</h4>.*?<p[^>]*>(?P<value>.*?)</p>",
    re.IGNORECASE | re.DOTALL,
)
# The RUC row spells it with an accent, Tipo Contribuyente without one.
_SUCESION_RE = re.compile(r"SUCESI[ÓO]N\s+INDIVISA", re.IGNORECASE)
_SUCESION_PREFIX_RE = re.compile(r"^SUCESI[ÓO]N\s+INDIVISA\s+", re.IGNORECASE)

_RESULT_MARKER = "Resultado de la B"
_ERROR_MARKERS = ("Pagina de Error", "Surgieron problemas")
# SUNAT answers an unregistered RUC with a result page saying the number "no es
# valido". It will never resolve, so this is an answer and retrying burns attempts.
# Matched up to the accent, which may arrive raw or escaped.
_NOT_REGISTERED_RE = re.compile(r"RUC\s+\d+\s+consultado no es v", re.IGNORECASE)


def parse_tipo_documento(page: str) -> SunatRecord | None:
    # None means a well-formed page carries no usable record, which the site's
    # allows_empty=False then reports as drift.
    ensure_no_error_page(page)
    if _NOT_REGISTERED_RE.search(page):
        msg = "sunat reports the ruc is not valid"
        raise RucNotFoundError(msg)
    if _RESULT_MARKER not in page:
        msg = "sunat response is not a result page"
        raise ProviderSchemaError(msg)

    tipo_contribuyente = _field(_TIPO_CONTRIBUYENTE_RE, page)
    record = _from_document_row(page, tipo_contribuyente)
    if record is not None:
        return record

    # No document row. Legitimate only for a sucesion indivisa, the estate of someone
    # who died intestate: SUNAT taxes it like a natural person but it holds no
    # identity document, and only the RUC row carries the name. Any other contributor
    # type with a missing row is parser drift, so report nothing and let it raise.
    if not _SUCESION_RE.search(tipo_contribuyente):
        return None
    # The RUC row repeats the "SUCESION INDIVISA" prefix that tipo_contribuyente
    # already carries; drop it so nombre holds only the person's name.
    nombre = _SUCESION_PREFIX_RE.sub("", _nombre_from_ruc_row(page)).strip()
    if not nombre:
        return None
    return SunatRecord(
        tipo_doc="",
        num_doc="",
        nombre=nombre,
        tipo_contribuyente=tipo_contribuyente,
    )


def _from_document_row(page: str, tipo_contribuyente: str) -> SunatRecord | None:
    match = _TIPO_DOC_RE.search(page)
    if match is None:
        return None
    text = clean(match.group("value"))
    if not text or text == "-":
        return None

    doc_part, _, nombre = text.partition(" - ")
    tokens = doc_part.split()
    if not tokens:
        return None
    # The document number is the trailing token; the type is everything before it.
    if len(tokens) == 1:
        return SunatRecord(
            tipo_doc=tokens[0],
            num_doc="",
            nombre=nombre.strip(),
            tipo_contribuyente=tipo_contribuyente,
        )
    return SunatRecord(
        tipo_doc=" ".join(tokens[:-1]),
        num_doc=tokens[-1],
        nombre=nombre.strip(),
        tipo_contribuyente=tipo_contribuyente,
    )


def _nombre_from_ruc_row(page: str) -> str:
    match = _RUC_ROW_RE.search(page)
    if match is None:
        return ""
    _, _, nombre = clean(match.group("value")).partition(" - ")
    return nombre.strip()


def _field(pattern: re.Pattern[str], page: str) -> str:
    match = pattern.search(page)
    return clean(match.group("value")) if match is not None else ""


def ensure_no_error_page(page: str) -> None:
    if any(marker in page for marker in _ERROR_MARKERS):
        msg = "sunat returned an error page"
        raise ProviderSchemaError(msg)


def clean(value: str) -> str:
    return " ".join(html_lib.unescape(value).split())
