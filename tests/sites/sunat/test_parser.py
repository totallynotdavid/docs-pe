from __future__ import annotations

import pytest

from robot.domain.errors import ProviderSchemaError
from robot.sites.sunat.parser import (
    RepRecord,
    SunatRecord,
    parse_razon_social,
    parse_reps,
    parse_tipo_documento,
)


def _result_page(value_block: str) -> str:
    return (
        "<html><body><h2>Resultado de la Búsqueda</h2>"
        "<h4>Tipo de Documento:</h4>"
        f'<div><p class="list-group-item-text">{value_block}</p></div>'
        "</body></html>"
    )


def _ficha_page(value_block: str) -> str:
    return (
        "<html><body><h4>N&uacute;mero de RUC:</h4>"
        f"<h4>{value_block}</h4></body></html>"
    )


def _reps_page(*rows: tuple[str, str, str, str, str]) -> str:
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<html><body><table><tbody>{body}</tbody></table></body></html>"


@pytest.mark.parametrize("marker", ["Pagina de Error", "Surgieron problemas"])
def test_an_error_page_raises_for_every_extractor(marker: str) -> None:
    page = f"<html>{marker}</html>"
    with pytest.raises(ProviderSchemaError, match="error page"):
        parse_tipo_documento(page)
    with pytest.raises(ProviderSchemaError, match="error page"):
        parse_razon_social(page)
    with pytest.raises(ProviderSchemaError, match="error page"):
        parse_reps(page)


def test_tipo_documento_extracts_document_type_number_and_name() -> None:
    record = parse_tipo_documento(_result_page("DNI  19187661  - JUAN PEREZ"))
    assert record == SunatRecord(
        tipo_doc="DNI", num_doc="19187661", nombre="JUAN PEREZ"
    )


def test_tipo_documento_preserves_multi_word_document_types() -> None:
    # The doc number is the trailing token, so a multi-word type must survive intact.
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


def test_tipo_documento_no_document_row_is_a_valid_empty() -> None:
    # RUC-20 companies return a real result page with no document row: an empty
    # success, not an error to retry.
    page = "<html><body>Resultado de la Búsqueda, sin documento</body></html>"
    assert parse_tipo_documento(page) is None


def test_tipo_documento_a_dash_only_value_is_a_valid_empty() -> None:
    assert parse_tipo_documento(_result_page("-")) is None


def test_tipo_documento_an_unrecognized_page_raises() -> None:
    with pytest.raises(ProviderSchemaError, match="not a result page"):
        parse_tipo_documento("<html>something unexpected</html>")


def test_razon_social_extracts_the_company_name() -> None:
    assert parse_razon_social(_ficha_page("20100000001 - ACME SAC")) == "ACME SAC"


def test_razon_social_unescapes_html_entities_in_the_name() -> None:
    assert parse_razon_social(_ficha_page("20100000001 - A &amp; B SAC")) == "A & B SAC"


def test_razon_social_a_missing_header_raises() -> None:
    with pytest.raises(ProviderSchemaError, match="missing the razon social"):
        parse_razon_social("<html><body>nothing here</body></html>")


def test_razon_social_a_value_without_a_separator_raises() -> None:
    with pytest.raises(ProviderSchemaError, match="malformed"):
        parse_razon_social(_ficha_page("20100000001 ACME SAC"))


def test_reps_extracts_every_representative() -> None:
    reps = parse_reps(
        _reps_page(
            ("DNI", "12345678", "JUAN PEREZ", "GERENTE", "01/01/2020"),
            ("DNI", "87654321", "ANA GOMEZ", "APODERADO", "02/02/2021"),
        )
    )
    assert reps == (
        RepRecord(
            doc_type="DNI",
            num_doc="12345678",
            nombre="JUAN PEREZ",
            cargo="GERENTE",
            fecha_desde="01/01/2020",
        ),
        RepRecord(
            doc_type="DNI",
            num_doc="87654321",
            nombre="ANA GOMEZ",
            cargo="APODERADO",
            fecha_desde="02/02/2021",
        ),
    )


def test_reps_a_row_with_the_wrong_cell_count_is_skipped() -> None:
    page = (
        "<html><body><table><tbody>"
        "<tr><td>only</td><td>two</td></tr>"
        "<tr><td>DNI</td><td>1</td><td>N</td><td>C</td><td>D</td></tr>"
        "</tbody></table></body></html>"
    )
    assert len(parse_reps(page)) == 1


def test_reps_no_results_table_raises() -> None:
    with pytest.raises(ProviderSchemaError, match="no results table"):
        parse_reps("<html><body>no table here</body></html>")


def test_reps_a_results_table_with_no_rows_is_a_valid_empty() -> None:
    assert parse_reps("<html><body><tbody></tbody></body></html>") == ()
