from __future__ import annotations

import pytest

from fetch.domain.errors import ProviderSchemaError
from fetch.sites.sunat.reps import RepRecord, build_reps_body, parse_reps


def _reps_page(*rows: tuple[str, str, str, str, str]) -> str:
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return f"<html><body><table><tbody>{body}</tbody></table></body></html>"


def test_reps_body_has_the_fixed_fields_and_an_empty_des_ruc() -> None:
    body = build_reps_body(ruc="20100000001")
    assert body == {
        "accion": "getRepLeg",
        "contexto": "ti-it",
        "modo": "1",
        "desRuc": "",
        "nroRuc": "20100000001",
    }


@pytest.mark.parametrize("marker", ["Pagina de Error", "Surgieron problemas"])
def test_an_error_page_raises(marker: str) -> None:
    with pytest.raises(ProviderSchemaError, match="error page"):
        parse_reps(f"<html>{marker}</html>")


def test_the_no_representatives_marker_is_a_valid_empty_without_a_table() -> None:
    page = "<html>No se encontro información para representantes legales</html>"
    assert parse_reps(page) == ()


def test_extracts_every_representative() -> None:
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


def test_a_row_with_the_wrong_cell_count_is_skipped() -> None:
    page = (
        "<html><body><table><tbody>"
        "<tr><td>only</td><td>two</td></tr>"
        "<tr><td>DNI</td><td>1</td><td>N</td><td>C</td><td>D</td></tr>"
        "</tbody></table></body></html>"
    )
    assert len(parse_reps(page)) == 1


def test_no_results_table_raises() -> None:
    with pytest.raises(ProviderSchemaError, match="no results table"):
        parse_reps("<html><body>no table here</body></html>")


def test_a_results_table_with_no_rows_is_a_valid_empty() -> None:
    assert parse_reps("<html><body><tbody></tbody></body></html>") == ()
