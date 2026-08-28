from __future__ import annotations

import csv
import re

from typing import TYPE_CHECKING

from fetch.domain.types import Doc, Projection, Result, Status
from fetch.store.export import ERROR_HEADERS, export_all, export_site, site_csv_path

from tests.fetch.conftest import fake_site as _site


if TYPE_CHECKING:
    from pathlib import Path

    from fetch.domain.types import Row
    from fetch.store.outcomes import OutcomeStore


def _success(site: str, doc: str, rows: tuple[Row, ...]) -> Result:
    return Result(doc=Doc(doc), site=site, status=Status.OK, rows=rows)


def _not_found(site: str, doc: str) -> Result:
    return Result(doc=Doc(doc), site=site, status=Status.NOT_FOUND, attempt=1)


def _failure(site: str, doc: str) -> Result:
    return Result(
        doc=Doc(doc),
        site=site,
        status=Status.FAILED,
        error_code="ban_signal",
        error_detail="blocked",
        attempt=4,
        http_session_id="sess",
        proxy_id="proxy",
        provider="geonode",
    )


def _read_rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as file_obj:
        return list(csv.reader(file_obj))


def test_export_site_writes_success_rows_under_the_sites_columns(
    store: OutcomeStore, tmp_path: Path
) -> None:
    store.record_success(
        _success("osiptel", "20100000001", (("POSTPAGO", "98857****", "CLARO"),))
    )
    output_csv = tmp_path / "out.csv"
    site = _site("osiptel", "modalidad", "numero", "operador")

    export_site(store=store, output_csv=output_csv, site=site)

    rows = _read_rows(site_csv_path(output_csv, "osiptel"))
    assert rows == [
        ["doc", "modalidad", "numero", "operador"],
        ["20100000001", "POSTPAGO", "98857****", "CLARO"],
    ]


def test_export_site_writes_each_projection_to_its_own_file(
    store: OutcomeStore, tmp_path: Path
) -> None:
    def project_counts(rows: tuple[Row, ...]) -> tuple[Row, ...]:
        return ((rows[0][2], len(rows), len(rows)),)

    store.record_success(
        _success(
            "osiptel",
            "20100000001",
            (
                ("POSTPAGO", "98857****", "CLARO"),
                ("PREPAGO", "96222****", "CLARO"),
            ),
        )
    )
    output_csv = tmp_path / "out.csv"
    projection = Projection(
        name="counts",
        columns=("carrier", "lines", "total_lines"),
        project=project_counts,
    )
    site = _site(
        "osiptel", "modalidad", "numero", "operador", projections=(projection,)
    )

    export_site(store=store, output_csv=output_csv, site=site)

    assert _read_rows(
        site_csv_path(output_csv, "osiptel").with_name("out.osiptel.counts.csv")
    ) == [
        ["doc", "carrier", "lines", "total_lines"],
        ["20100000001", "CLARO", "2", "2"],
    ]


def test_an_empty_success_payload_writes_no_data_row(
    store: OutcomeStore, tmp_path: Path
) -> None:
    store.record_success(_success("sunat", "20100000001", ()))
    output_csv = tmp_path / "out.csv"
    site = _site("sunat", "tipo_doc", "num_doc", "nombre")

    export_site(store=store, output_csv=output_csv, site=site)

    rows = _read_rows(site_csv_path(output_csv, "sunat"))
    assert rows == [["doc", "tipo_doc", "num_doc", "nombre"]]


def test_export_site_writes_error_rows_with_the_fixed_headers(
    store: OutcomeStore, tmp_path: Path
) -> None:
    store.record_failure(_failure("osiptel", "20100000001"))
    output_csv = tmp_path / "out.csv"
    site = _site("osiptel", "modalidad", "numero", "operador")

    export_site(store=store, output_csv=output_csv, site=site)

    rows = _read_rows(site_csv_path(output_csv, "osiptel").with_suffix(".errors.csv"))
    assert rows[0] == ERROR_HEADERS
    assert len(rows) == 2
    assert rows[1][:6] == [
        "20100000001",
        "ban_signal",
        "blocked",
        "4",
        "sess",
        "proxy",
    ]
    assert rows[1][6] == "geonode"
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(\+\d{2}:\d{2}|Z)?",
        rows[1][7],
    )


def test_export_site_writes_not_found_rows(store: OutcomeStore, tmp_path: Path) -> None:
    store.record_not_found(_not_found("sunat_reps", "20100000001"))
    output_csv = tmp_path / "out.csv"
    site = _site("sunat_reps", "razon_social")

    export_site(store=store, output_csv=output_csv, site=site)

    rows = _read_rows(
        site_csv_path(output_csv, "sunat_reps").with_suffix(".not_found.csv")
    )
    assert rows[0] == ["doc", "timestamp"]
    assert rows[1][0] == "20100000001"


def test_export_all_writes_every_site(store: OutcomeStore, tmp_path: Path) -> None:
    store.record_success(
        _success("osiptel", "20100000001", (("POSTPAGO", "98857****", "CLARO"),))
    )
    store.record_success(_success("sunat", "20100000002", ()))
    output_csv = tmp_path / "out.csv"
    sites = [
        _site("osiptel", "modalidad", "numero", "operador"),
        _site("sunat", "tipo_doc", "num_doc", "nombre"),
    ]

    export_all(store=store, output_csv=output_csv, sites=sites)

    osiptel_rows = _read_rows(site_csv_path(output_csv, "osiptel"))
    assert osiptel_rows == [
        ["doc", "modalidad", "numero", "operador"],
        ["20100000001", "POSTPAGO", "98857****", "CLARO"],
    ]

    sunat_rows = _read_rows(site_csv_path(output_csv, "sunat"))
    assert sunat_rows == [["doc", "tipo_doc", "num_doc", "nombre"]]
