from __future__ import annotations

import html as html_lib
import re

from dataclasses import dataclass

from fetch.domain.errors import ProviderSchemaError


@dataclass(frozen=True)
class SunatRecord:
    tipo_doc: str
    num_doc: str
    nombre: str


# The value block: <h4>Tipo de Documento:</h4> ... <p ...>DNI  19187661  - NAME</p>
_TIPO_DOC_RE = re.compile(
    r"Tipo de Documento:\s*</h4>.*?<p[^>]*>(?P<value>.*?)</p>",
    re.IGNORECASE | re.DOTALL,
)

_RESULT_MARKER = "Resultado de la B"
_ERROR_MARKERS = ("Pagina de Error", "Surgieron problemas")


def parse_tipo_documento(page: str) -> SunatRecord | None:
    # None means a well-formed page carries no document row.
    ensure_no_error_page(page)
    if _RESULT_MARKER not in page:
        msg = "sunat response is not a result page"
        raise ProviderSchemaError(msg)

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
        return SunatRecord(tipo_doc=tokens[0], num_doc="", nombre=nombre.strip())
    return SunatRecord(
        tipo_doc=" ".join(tokens[:-1]),
        num_doc=tokens[-1],
        nombre=nombre.strip(),
    )


def ensure_no_error_page(page: str) -> None:
    if any(marker in page for marker in _ERROR_MARKERS):
        msg = "sunat returned an error page"
        raise ProviderSchemaError(msg)


def clean(value: str) -> str:
    return " ".join(html_lib.unescape(value).split())
