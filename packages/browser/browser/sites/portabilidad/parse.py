from __future__ import annotations

import html as html_lib
import re

from browser.errors import BrowserError


RESULT_MARKER = "Número consultado"
CAPTCHA_ERROR = "Captcha incorrecto"
_PORTED_STATUS = "Número portado"

# Result fields are rendered as adjacent label and value cells.
_ROW_RE = re.compile(
    r"<td>\s*<strong>\s*(?P<label>[^<:]+?)\s*:\s*</strong>\s*</td>\s*"
    r"<td>\s*(?P<value>.*?)\s*</td>",
    re.IGNORECASE | re.DOTALL,
)

_FIELD_BY_LABEL = {
    "Número": "number",
    "Receptor": "receptor",
    "Cedente": "cedente",
    "Asignatario Original": "asignatario_original",
    "Fecha de la ventana": "fecha_ventana",
    "Estado": "estado",
}

# Unported numbers may omit Cedente and the window date.
_REQUIRED_FIELDS = ("number", "receptor", "asignatario_original", "estado")


def parse_result(page: str, *, expected_number: str) -> dict[str, str]:
    if RESULT_MARKER not in page:
        msg = "portabilidad response is not a result page"
        raise BrowserError(msg)

    fields: dict[str, str] = {}

    for match in _ROW_RE.finditer(page):
        key = _FIELD_BY_LABEL.get(_clean(match.group("label")))

        if key is not None:
            fields[key] = _clean(match.group("value"))

    for key in _REQUIRED_FIELDS:
        if not fields.get(key):
            msg = f"portabilidad result is missing {key}"
            raise BrowserError(msg)

    if fields["number"] != expected_number:
        msg = "portabilidad result is for another number"
        raise BrowserError(msg)

    fields["current_carrier"] = _current_carrier(fields)

    return fields


def _current_carrier(fields: dict[str, str]) -> str:
    if fields["estado"] == _PORTED_STATUS:
        return fields["receptor"]

    return fields["asignatario_original"]


def _clean(value: str) -> str:
    return " ".join(html_lib.unescape(value).split())
