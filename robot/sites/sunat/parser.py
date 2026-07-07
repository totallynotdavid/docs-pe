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


# The value block: <h4>Tipo de Documento:</h4> ... <p ...>DNI  19187661  - NAME</p>
_TIPO_DOC_RE = re.compile(
    r"Tipo de Documento:\s*</h4>.*?<p[^>]*>(?P<value>.*?)</p>",
    re.IGNORECASE | re.DOTALL,
)
_RESULT_MARKER = "Resultado de la B"
_ERROR_MARKERS = ("Pagina de Error", "Surgieron problemas")


def parse_page(page: str) -> SunatRecord | None:
    # Distinguish a real result page with no document row (RUC-20 companies) from an
    # error page: the former is a valid empty success, the latter must retry.
    if any(marker in page for marker in _ERROR_MARKERS):
        msg = "sunat returned an error page"
        raise ProviderSchemaError(msg)
    if _RESULT_MARKER not in page:
        msg = "sunat response is not a result page"
        raise ProviderSchemaError(msg)
    return _parse_tipo_documento(page)


def _parse_tipo_documento(page: str) -> SunatRecord | None:
    match = _TIPO_DOC_RE.search(page)
    if match is None:
        return None
    text = " ".join(html_lib.unescape(match.group("value")).split())
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
