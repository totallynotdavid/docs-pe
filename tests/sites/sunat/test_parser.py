from __future__ import annotations

import pytest

from robot.domain.errors import ProviderSchemaError
from robot.sites.sunat.parser import SunatRecord, parse_page


def _result_page(value_block: str) -> str:
    return (
        "<html><body><h2>Resultado de la Búsqueda</h2>"
        "<h4>Tipo de Documento:</h4>"
        f'<div><p class="list-group-item-text">{value_block}</p></div>'
        "</body></html>"
    )


def test_extracts_document_type_number_and_name() -> None:
    record = parse_page(_result_page("DNI  19187661  - JUAN PEREZ"))
    assert record == SunatRecord(
        tipo_doc="DNI", num_doc="19187661", nombre="JUAN PEREZ"
    )


def test_preserves_multi_word_document_types() -> None:
    # The doc number is the trailing token, so a multi-word type must survive intact.
    record = parse_page(_result_page("Carnet de Extranjeria  001234  - MARIA LOPEZ"))
    assert record == SunatRecord(
        tipo_doc="Carnet de Extranjeria", num_doc="001234", nombre="MARIA LOPEZ"
    )


def test_unescapes_html_entities_in_the_name() -> None:
    record = parse_page(_result_page("DNI  1  - A &amp; B"))
    assert record is not None
    assert record.nombre == "A & B"


def test_a_single_token_value_has_no_document_number() -> None:
    record = parse_page(_result_page("PASAPORTE"))
    assert record == SunatRecord(tipo_doc="PASAPORTE", num_doc="", nombre="")


def test_a_result_page_without_a_document_row_is_a_valid_empty() -> None:
    # RUC-20 companies return a real result page with no document row: an empty
    # success, not an error to retry.
    page = "<html><body>Resultado de la Búsqueda, sin documento</body></html>"
    assert parse_page(page) is None


def test_a_dash_only_value_is_a_valid_empty() -> None:
    assert parse_page(_result_page("-")) is None


@pytest.mark.parametrize("marker", ["Pagina de Error", "Surgieron problemas"])
def test_an_error_page_raises(marker: str) -> None:
    with pytest.raises(ProviderSchemaError, match="error page"):
        parse_page(f"<html>{marker}</html>")


def test_an_unrecognized_page_raises() -> None:
    with pytest.raises(ProviderSchemaError, match="not a result page"):
        parse_page("<html>something unexpected</html>")
