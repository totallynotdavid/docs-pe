from __future__ import annotations

import csv
import json

from pathlib import Path
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs

import pytest

from browser.errors import BrowserError
from browser.sites.registry import SITES
from browser.sites.sunat.page import SunatIdentityPage, SunatRepsPage
from browser.sites.sunat.parse import (
    Identity,
    Representative,
    parse_identity,
    parse_razon_social,
    parse_representatives,
)
from browser.store import ObservationStore
from browser.subject import Subject


if TYPE_CHECKING:
    from browser.session import Session


# Pages SUNAT returned to the browser, with personal data replaced.
_FIXTURES = Path(__file__).parent / "fixtures" / "sunat"
_RUC_NATURAL = "10123456786"
_RUC_JURIDICA = "20100070970"
_RUC_UNREGISTERED = "10000000014"
_RUC_WITHOUT_REPS = "20100070112"


def _fixture(name: str) -> str:
    return (_FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def _page(
    *,
    documento: str | None = None,
    ruc_row: str | None = None,
    tipo_contribuyente: str | None = None,
) -> str:
    parts = ["<html><body><h2>Resultado de la Búsqueda</h2>"]

    if ruc_row is not None:
        parts.append(
            '<h4 class="list-group-item-heading">N&uacute;mero de RUC:</h4>'
            '<div class="col-sm-7"><h4 class="list-group-item-heading">'
            f"{ruc_row}</h4></div>"
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


def test_parses_a_natural_person_page() -> None:
    assert parse_identity(_fixture("natural")) == Identity(
        tipo_doc="DNI",
        num_doc="12345678",
        nombre="PEREZ GOMEZ, MARIA",
        tipo_contribuyente="PERSONA NATURAL SIN NEGOCIO",
    )


def test_parses_a_legal_entity_name_from_the_ruc_row() -> None:
    assert parse_razon_social(_fixture("juridica")) == (
        "SUPERMERCADOS PERUANOS SOCIEDAD ANONIMA 'O ' S.P.S.A."
    )


@pytest.mark.parametrize("parse", [parse_identity, parse_razon_social])
def test_an_unregistered_ruc_is_none_rather_than_an_error(parse) -> None:  # type: ignore[no-untyped-def]
    assert parse(_fixture("unregistered")) is None


def test_parses_every_representative_across_document_types() -> None:
    assert parse_representatives(_fixture("reps")) == (
        Representative(
            "DNI", "12345678", "PEREZ GOMEZ MARIA", "APODERADO", "08/04/2013"
        ),
        Representative("CE", "001234567", "DOE JANE", "APODERADO", "27/08/2022"),
        Representative(
            "RUC",
            "20123456786",
            "EMPRESA EJEMPLO S.A.C.",
            "GERENTE GENERAL",
            "27/08/2022",
        ),
    )


def test_the_no_representatives_error_page_is_a_valid_empty() -> None:
    # SUNAT reports an empty list with its generic error page.
    assert parse_representatives(_fixture("no_reps")) == ()


@pytest.mark.parametrize(
    ("page", "expected"),
    [
        pytest.param(
            _page(documento="DNI  19187661  - JUAN PEREZ"),
            Identity("DNI", "19187661", "JUAN PEREZ", ""),
            id="document row",
        ),
        pytest.param(
            _page(documento="Carnet de Extranjeria  001234  - MARIA LOPEZ"),
            Identity("Carnet de Extranjeria", "001234", "MARIA LOPEZ", ""),
            id="multi-word document type",
        ),
        pytest.param(
            _page(documento="DNI  1  - A &amp; B"),
            Identity("DNI", "1", "A & B", ""),
            id="entities unescaped",
        ),
        pytest.param(
            _page(
                ruc_row="10000002301 - SUCESIÓN INDIVISA QUIROZ VASQUEZ VDA DE GONZALES",
                tipo_contribuyente="SUCESION INDIVISA SIN NEGOCIO",
            ),
            Identity(
                "",
                "",
                "QUIROZ VASQUEZ VDA DE GONZALES",
                "SUCESION INDIVISA SIN NEGOCIO",
            ),
            id="sucesion indivisa without a document row",
        ),
        pytest.param(
            _page(
                ruc_row="10000002301 - SUCESION INDIVISA PEREZ GOMEZ",
                tipo_contribuyente="SUCESION INDIVISA CON NEGOCIO",
            ),
            Identity("", "", "PEREZ GOMEZ", "SUCESION INDIVISA CON NEGOCIO"),
            id="sucesion indivisa with a business",
        ),
    ],
)
def test_identity_variants(page: str, expected: Identity) -> None:
    assert parse_identity(page) == expected


@pytest.mark.parametrize(
    ("page", "message"),
    [
        pytest.param(
            _page(
                ruc_row="10000020830 - NAKAYA NOLAZCO JOSE LUIS",
                tipo_contribuyente="PERSONA NATURAL SIN NEGOCIO",
            ),
            "no document row",
            id="normal contributor without a document row",
        ),
        pytest.param(_page(documento="-"), "no document row", id="dash document row"),
        pytest.param("<html>unexpected</html>", "not a result page", id="other page"),
        pytest.param("<html>Pagina de Error</html>", "error page", id="error page"),
        pytest.param(
            "<html>Surgieron problemas</html>", "error page", id="problems page"
        ),
    ],
)
def test_response_drift_is_a_browser_error(page: str, message: str) -> None:
    with pytest.raises(BrowserError, match=message):
        parse_identity(page)


def test_a_representatives_page_without_a_table_is_a_browser_error() -> None:
    with pytest.raises(BrowserError, match="no results table"):
        parse_representatives("<html><body>no table here</body></html>")


class _FixtureSession:
    """Answers SUNAT form posts from saved pages, as the browser would."""

    def __init__(self, pages: dict[tuple[str, str], str]) -> None:
        self._pages = pages
        self.posts: list[dict[str, list[str]]] = []

    def is_element_present(self, selector: str) -> bool:
        return True

    def sleep(self, seconds: float) -> None:
        pass

    def evaluate(self, expression: str) -> str:
        literal = expression.split("request.send(", 1)[1].split(");", 1)[0]
        body = parse_qs(json.loads(literal), keep_blank_values=True)
        self.posts.append(body)
        page = self._pages[body["accion"][0], body["nroRuc"][0]]

        return json.dumps([200, page])


def _session() -> _FixtureSession:
    return _FixtureSession(
        {
            ("consPorRuc", _RUC_NATURAL): _fixture("natural"),
            ("consPorRuc", _RUC_JURIDICA): _fixture("juridica"),
            ("consPorRuc", _RUC_UNREGISTERED): _fixture("unregistered"),
            ("consPorRuc", _RUC_WITHOUT_REPS): _fixture("juridica"),
            ("getRepLeg", _RUC_JURIDICA): _fixture("reps"),
            ("getRepLeg", _RUC_WITHOUT_REPS): _fixture("no_reps"),
        }
    )


def _export(site_name: str, subject: str, page_class: type, tmp_path: Path):  # type: ignore[no-untyped-def]
    site = SITES[site_name]
    session = _session()
    page = page_class(session=cast("Session", session))
    result = page.lookup(subject)
    out = tmp_path / "out.csv"

    with ObservationStore(tmp_path / "state.sqlite3") as store:
        store.record_success(
            run_id="r", site=site_name, subject=subject, columns=result.columns
        )
        store.export_current(
            out, site=site_name, header=site.export_header, project=site.rows
        )

    with out.open(newline="", encoding="utf-8") as file_obj:
        return session, list(csv.reader(file_obj))


def test_sunat_exports_one_row_for_a_natural_person(tmp_path: Path) -> None:
    session, rows = _export("sunat", _RUC_NATURAL, SunatIdentityPage, tmp_path)

    assert [post["accion"] for post in session.posts] == [["consPorRuc"]]
    assert rows[1][:5] == [
        _RUC_NATURAL,
        "DNI",
        "12345678",
        "PEREZ GOMEZ, MARIA",
        "PERSONA NATURAL SIN NEGOCIO",
    ]
    assert len(rows) == 2


def test_sunat_reps_exports_one_row_per_representative(tmp_path: Path) -> None:
    session, rows = _export("sunat_reps", _RUC_JURIDICA, SunatRepsPage, tmp_path)

    assert [post["accion"] for post in session.posts] == [["consPorRuc"], ["getRepLeg"]]
    assert session.posts[1]["desRuc"] == [""]
    assert [row[1:7] for row in rows[1:]] == [
        [
            "SUPERMERCADOS PERUANOS SOCIEDAD ANONIMA 'O ' S.P.S.A.",
            "DNI",
            "12345678",
            "PEREZ GOMEZ MARIA",
            "APODERADO",
            "08/04/2013",
        ],
        [
            "SUPERMERCADOS PERUANOS SOCIEDAD ANONIMA 'O ' S.P.S.A.",
            "CE",
            "001234567",
            "DOE JANE",
            "APODERADO",
            "27/08/2022",
        ],
        [
            "SUPERMERCADOS PERUANOS SOCIEDAD ANONIMA 'O ' S.P.S.A.",
            "RUC",
            "20123456786",
            "EMPRESA EJEMPLO S.A.C.",
            "GERENTE GENERAL",
            "27/08/2022",
        ],
    ]


@pytest.mark.parametrize(
    ("site_name", "page_class", "subject", "posts"),
    [
        pytest.param(
            "sunat", SunatIdentityPage, _RUC_UNREGISTERED, 1, id="sunat unregistered"
        ),
        pytest.param(
            "sunat_reps",
            SunatRepsPage,
            _RUC_UNREGISTERED,
            1,
            id="reps unregistered skips the reps request",
        ),
        pytest.param(
            "sunat_reps",
            SunatRepsPage,
            _RUC_WITHOUT_REPS,
            2,
            id="reps without representatives",
        ),
    ],
)
def test_a_final_answer_without_data_exports_no_rows(
    site_name: str, page_class: type, subject: str, posts: int, tmp_path: Path
) -> None:
    session, rows = _export(site_name, subject, page_class, tmp_path)

    assert len(session.posts) == posts
    assert len(rows) == 1


def test_a_non_200_answer_is_a_browser_error() -> None:
    class Blocked(_FixtureSession):
        def evaluate(self, expression: str) -> str:
            return json.dumps([403, "<html>Just a moment...</html>"])

    page = SunatIdentityPage(session=cast("Session", Blocked({})))

    with pytest.raises(BrowserError, match="HTTP 403"):
        page.lookup(_RUC_NATURAL)


def test_sites_route_rucs_by_prefix() -> None:
    natural, juridica = Subject(_RUC_NATURAL), Subject(_RUC_JURIDICA)

    assert SITES["sunat"].accepts(natural)
    assert not SITES["sunat"].accepts(juridica)
    assert SITES["sunat_reps"].accepts(juridica)
    assert not SITES["sunat_reps"].accepts(natural)
    assert not SITES["sunat"].accepts(Subject("12345678"))
