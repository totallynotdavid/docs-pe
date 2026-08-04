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


def parse_tipo_documento(page: str) -> SunatRecord | None:
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

    if not _SUCESION_RE.search(tipo_contribuyente):
        return None

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
