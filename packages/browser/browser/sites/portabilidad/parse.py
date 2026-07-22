from __future__ import annotations

import html as html_lib
import re

from typing import NoReturn

from browser.errors import BrowserError


# The result card the server renders after a valid submit; its absence means the
# lookup did not complete (or the token was rejected, detected earlier by the page).
RESULT_MARKER = "Número consultado"
CAPTCHA_ERROR = "Captcha incorrecto"
_PORTED = "Número portado"

# Each result row is <td><strong>Label:</strong></td> <td>value</td>.
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
# A not-ported number still returns a valid card (Receptor "-"), so Cedente and the
# window date can be blank; these four must always be present.
_REQUIRED = ("number", "receptor", "asignatario_original", "estado")


def parse_result(page: str, *, expected_number: str) -> dict[str, str]:
    if RESULT_MARKER not in page:
        _fail("portabilidad response is not a result page")

    fields: dict[str, str] = {}
    for match in _ROW_RE.finditer(page):
        key = _FIELD_BY_LABEL.get(_clean(match.group("label")))
        if key is not None:
            fields[key] = _clean(match.group("value"))

    for key in _REQUIRED:
        if not fields.get(key):
            _fail(f"portabilidad result is missing {key}")
    if fields["number"] != expected_number:
        _fail("portabilidad result is for another number")

    fields["current_carrier"] = _current_carrier(fields)
    return fields


def _current_carrier(fields: dict[str, str]) -> str:
    # A ported number now belongs to the Receptor; an unported one is still with its
    # original assignee.
    if fields["estado"] == _PORTED:
        return fields["receptor"]
    return fields["asignatario_original"]


def _clean(value: str) -> str:
    return " ".join(html_lib.unescape(value).split())


def _fail(message: str) -> NoReturn:
    raise BrowserError(message)
