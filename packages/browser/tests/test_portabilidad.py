from __future__ import annotations

from pathlib import Path

import pytest

from browser.errors import BrowserError
from browser.sites.portabilidad.parse import parse_result
from browser.sites.registry import SITES
from browser.subject import Subject


_SAMPLE = Path(__file__).resolve().parents[3] / "example" / "returned.html"

# A not-ported number still returns a valid card: Receptor "-", and the current
# carrier falls back to the original assignee.
_NOT_PORTED_HTML = """
<div class="card">
  <div class="card-header"><strong>Número consultado</strong></div>
  <div class="card-body">
    <table class="table table-striped"><tbody>
      <tr><td><strong>Número:</strong></td><td>912345678</td></tr>
      <tr><td><strong>Receptor:</strong></td><td>-</td></tr>
      <tr><td><strong>Cedente:</strong></td><td>-</td></tr>
      <tr><td><strong>Asignatario Original:</strong></td><td>Entel Perú S.A.</td></tr>
      <tr><td><strong>Fecha de la ventana:</strong></td><td>-</td></tr>
      <tr><td><strong>Estado:</strong></td><td>Número no portado</td></tr>
    </tbody></table>
  </div>
</div>
"""


def test_parses_ported_number_from_the_real_sample() -> None:
    result = parse_result(
        _SAMPLE.read_text(encoding="utf-8"), expected_number="980080023"
    )
    assert result["receptor"] == "América Móvil Perú S.A.C. (Claro)"
    assert result["estado"] == "Número portado"
    assert result["current_carrier"] == "América Móvil Perú S.A.C. (Claro)"
    assert result["cedente"].startswith("Telefónica del Perú")


def test_not_ported_current_carrier_is_the_original_assignee() -> None:
    result = parse_result(_NOT_PORTED_HTML, expected_number="912345678")
    assert result["receptor"] == "-"
    assert result["estado"] == "Número no portado"
    assert result["current_carrier"] == "Entel Perú S.A."


def test_result_for_another_number_is_a_structural_error() -> None:
    with pytest.raises(BrowserError, match="another number"):
        parse_result(_NOT_PORTED_HTML, expected_number="999999999")


def test_non_result_page_is_a_structural_error() -> None:
    with pytest.raises(BrowserError, match="not a result page"):
        parse_result("<html><body>nada</body></html>", expected_number="912345678")


def test_portabilidad_accepts_only_phones() -> None:
    accepts = SITES["portabilidad"].accepts
    assert accepts(Subject("987654321"))
    assert not accepts(Subject("20131312955"))
    assert not accepts(Subject("12345678"))


def test_entel_accepts_dni_and_ruc_but_not_phones() -> None:
    accepts = SITES["entel"].accepts
    assert accepts(Subject("20131312955"))
    assert accepts(Subject("12345678"))
    assert not accepts(Subject("987654321"))
