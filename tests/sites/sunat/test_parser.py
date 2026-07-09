from __future__ import annotations

import pytest

from robot.domain.errors import ProviderSchemaError
from robot.sites.sunat.parser import (
    SunatRecord,
    clean,
    ensure_no_error_page,
    parse_tipo_documento,
)


def _result_page(value_block: str) -> str:
    return (
        "<html><body><h2>Resultado de la Búsqueda</h2>"
        "<h4>Tipo de Documento:</h4>"
        f'<div><p class="list-group-item-text">{value_block}</p></div>'
        "</body></html>"
    )


@pytest.mark.parametrize("marker", ["Pagina de Error", "Surgieron problemas"])
def test_ensure_no_error_page_raises_on_either_marker(marker: str) -> None:
    with pytest.raises(ProviderSchemaError, match="error page"):
        ensure_no_error_page(f"<html>{marker}</html>")


def test_ensure_no_error_page_passes_a_clean_page() -> None:
    ensure_no_error_page("<html>nothing wrong here</html>")


def test_clean_unescapes_entities_and_collapses_whitespace() -> None:
    assert clean("A &amp;  B\n  C") == "A & B C"


def test_tipo_documento_extracts_document_type_number_and_name() -> None:
    record = parse_tipo_documento(_result_page("DNI  19187661  - JUAN PEREZ"))
    assert record == SunatRecord(
        tipo_doc="DNI", num_doc="19187661", nombre="JUAN PEREZ"
    )


def test_tipo_documento_preserves_multi_word_document_types() -> None:
    # Doc number is the trailing token.
    record = parse_tipo_documento(
        _result_page("Carnet de Extranjeria  001234  - MARIA LOPEZ")
    )
    assert record == SunatRecord(
        tipo_doc="Carnet de Extranjeria", num_doc="001234", nombre="MARIA LOPEZ"
    )


def test_tipo_documento_unescapes_html_entities_in_the_name() -> None:
    record = parse_tipo_documento(_result_page("DNI  1  - A &amp; B"))
    assert record is not None
    assert record.nombre == "A & B"


def test_tipo_documento_a_single_token_value_has_no_document_number() -> None:
    record = parse_tipo_documento(_result_page("PASAPORTE"))
    assert record == SunatRecord(tipo_doc="PASAPORTE", num_doc="", nombre="")


@pytest.mark.parametrize(
    "page",
    [
        # RUC-20 companies return a real result page with no document row.
        "<html><body>Resultado de la Búsqueda, sin documento</body></html>",
        # A "-" placeholder is treated the same as a missing document row.
        _result_page("-"),
    ],
)
def test_tipo_documento_a_missing_document_row_is_a_valid_empty(page: str) -> None:
    assert parse_tipo_documento(page) is None


def test_tipo_documento_an_unrecognized_page_raises() -> None:
    with pytest.raises(ProviderSchemaError, match="not a result page"):
        parse_tipo_documento("<html>something unexpected</html>")


def test_tipo_documento_an_error_page_raises() -> None:
    with pytest.raises(ProviderSchemaError, match="error page"):
        parse_tipo_documento("<html>Pagina de Error</html>")
