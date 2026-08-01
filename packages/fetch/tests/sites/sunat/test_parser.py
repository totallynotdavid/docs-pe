from __future__ import annotations

import pytest

from fetch.domain.errors import ProviderSchemaError, RucNotFoundError
from fetch.sites.sunat.parser import (
    SunatRecord,
    clean,
    ensure_no_error_page,
    parse_tipo_documento,
)


def _page(
    *,
    documento: str | None = None,
    ruc_row: str | None = None,
    tipo_contribuyente: str | None = None,
) -> str:
    # Mirrors the real ficha markup: headings are <h4>, the RUC row's value is a
    # second <h4>, every other value is a <p class="list-group-item-text">.
    parts = ["<html><body><h2>Resultado de la Búsqueda</h2>"]
    if ruc_row is not None:
        parts.append(
            '<h4 class="list-group-item-heading">N&uacute;mero de RUC:</h4>'
            f'<div class="col-sm-7"><h4 class="list-group-item-heading">{ruc_row}</h4></div>'
        )
    if tipo_contribuyente is not None:
        parts.append(
            '<h4 class="list-group-item-heading">Tipo Contribuyente:</h4>'
            f'<div><p class="list-group-item-text">{tipo_contribuyente}</p></div>'
        )
    if documento is not None:
        parts.append(
            "<h4>Tipo de Documento:</h4>"
            f'<div><p class="list-group-item-text">{documento}</p></div>'
        )
    parts.append("</body></html>")
    return "".join(parts)


def _result_page(value_block: str) -> str:
    return _page(documento=value_block)


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
        tipo_doc="DNI", num_doc="19187661", nombre="JUAN PEREZ", tipo_contribuyente=""
    )


def test_tipo_documento_preserves_multi_word_document_types() -> None:
    # Doc number is the trailing token.
    record = parse_tipo_documento(
        _result_page("Carnet de Extranjeria  001234  - MARIA LOPEZ")
    )
    assert record == SunatRecord(
        tipo_doc="Carnet de Extranjeria",
        num_doc="001234",
        nombre="MARIA LOPEZ",
        tipo_contribuyente="",
    )


def test_tipo_documento_unescapes_html_entities_in_the_name() -> None:
    record = parse_tipo_documento(_result_page("DNI  1  - A &amp; B"))
    assert record is not None
    assert record.nombre == "A & B"


def test_tipo_documento_a_single_token_value_has_no_document_number() -> None:
    record = parse_tipo_documento(_result_page("PASAPORTE"))
    assert record == SunatRecord(
        tipo_doc="PASAPORTE", num_doc="", nombre="", tipo_contribuyente=""
    )


def test_tipo_contribuyente_is_captured_alongside_the_document() -> None:
    record = parse_tipo_documento(
        _page(
            documento="DNI  19187661  - JUAN PEREZ",
            tipo_contribuyente="PERSONA NATURAL SIN NEGOCIO",
        )
    )
    assert record is not None
    assert record.tipo_contribuyente == "PERSONA NATURAL SIN NEGOCIO"


def test_sucesion_indivisa_takes_its_name_from_the_ruc_row() -> None:
    # An estate has no identity document, but the RUC row still carries the name
    # of the deceased. The row's "SUCESIÓN INDIVISA" prefix duplicates
    # tipo_contribuyente, so nombre keeps only the person.
    record = parse_tipo_documento(
        _page(
            ruc_row="10000002301 - SUCESIÓN INDIVISA QUIROZ VASQUEZ VDA DE GONZALES",
            tipo_contribuyente="SUCESION INDIVISA SIN NEGOCIO",
        )
    )
    assert record == SunatRecord(
        tipo_doc="",
        num_doc="",
        nombre="QUIROZ VASQUEZ VDA DE GONZALES",
        tipo_contribuyente="SUCESION INDIVISA SIN NEGOCIO",
    )


def test_sucesion_indivisa_con_negocio_is_recognised_too() -> None:
    record = parse_tipo_documento(
        _page(
            ruc_row="10000002301 - SUCESION INDIVISA PEREZ GOMEZ",
            tipo_contribuyente="SUCESION INDIVISA CON NEGOCIO",
        )
    )
    assert record is not None
    assert record.nombre == "PEREZ GOMEZ"


def test_a_missing_document_row_for_a_normal_contributor_is_still_drift() -> None:
    # The whole point of keying on tipo_contribuyente: a persona natural with no
    # document row means the parser has drifted, and must not be reported as a
    # valid blank just because a name happens to be readable.
    page = _page(
        ruc_row="10000020830 - NAKAYA NOLAZCO JOSE LUIS",
        tipo_contribuyente="PERSONA NATURAL SIN NEGOCIO",
    )
    assert parse_tipo_documento(page) is None


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


@pytest.mark.parametrize(
    "sentence",
    [
        "El número de RUC 10436389651 consultado no es válido.",
        # The accents may arrive escaped rather than raw.
        "El n&uacute;mero de RUC 10436389651 consultado no es v&aacute;lido.",
    ],
)
def test_an_unregistered_ruc_is_not_found_rather_than_drift(sentence: str) -> None:
    # SUNAT serves this on a normal result page. Reporting it as drift would retry
    # a RUC that can never resolve until its attempts ran out.
    page = (
        f"<html><body><h2>Resultado de la Búsqueda</h2><p>{sentence}</p></body></html>"
    )
    with pytest.raises(RucNotFoundError, match="not valid"):
        parse_tipo_documento(page)


def test_a_valid_record_is_not_mistaken_for_an_unregistered_ruc() -> None:
    record = parse_tipo_documento(_result_page("DNI  19187661  - JUAN PEREZ"))
    assert record is not None


def test_tipo_documento_an_unrecognized_page_raises() -> None:
    with pytest.raises(ProviderSchemaError, match="not a result page"):
        parse_tipo_documento("<html>something unexpected</html>")


def test_tipo_documento_an_error_page_raises() -> None:
    with pytest.raises(ProviderSchemaError, match="error page"):
        parse_tipo_documento("<html>Pagina de Error</html>")
