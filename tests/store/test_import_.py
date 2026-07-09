from __future__ import annotations

from typing import TYPE_CHECKING

from robot.domain.types import RucKind, Site, SiteTuning
from robot.store.export import site_csv_path
from robot.store.import_ import import_site


if TYPE_CHECKING:
    from pathlib import Path

    import httpx

    from robot.domain.types import RUC, Row
    from robot.store.outcomes import OutcomeStore


def _site(name: str, *columns: str) -> Site:
    async def ready(client: httpx.AsyncClient, site: Site) -> None:  # noqa: RUF029
        return None

    async def lookup(client: httpx.AsyncClient, ruc: RUC) -> tuple[Row, ...]:  # noqa: RUF029
        return ()

    return Site(
        name=name,
        columns=columns,
        supports=frozenset({RucKind.JURIDICA}),
        allows_empty=True,
        tuning=SiteTuning(session_budget=1),
        endpoints=(),
        ready=ready,
        lookup=lookup,
    )


def test_a_missing_export_file_imports_nothing(
    store: OutcomeStore, tmp_path: Path
) -> None:
    site = _site("osiptel", "carrier", "lines", "total_lines")
    imported = import_site(store=store, output_csv=tmp_path / "out.csv", site=site)
    assert imported == 0


def test_an_empty_export_file_imports_nothing(
    store: OutcomeStore, tmp_path: Path
) -> None:
    site = _site("osiptel", "carrier", "lines", "total_lines")
    site_csv_path(tmp_path / "out.csv", site.name).touch()
    imported = import_site(store=store, output_csv=tmp_path / "out.csv", site=site)
    assert imported == 0


def test_a_header_mismatch_imports_nothing(store: OutcomeStore, tmp_path: Path) -> None:
    site = _site("osiptel", "carrier", "lines", "total_lines")
    path = site_csv_path(tmp_path / "out.csv", site.name)
    path.write_text("ruc,wrong,header\n20100000001,CLARO,1\n", encoding="utf-8")
    imported = import_site(store=store, output_csv=tmp_path / "out.csv", site=site)
    assert imported == 0
    assert list(store.success_rows("osiptel")) == []


def test_a_row_with_the_wrong_column_count_is_skipped(
    store: OutcomeStore, tmp_path: Path
) -> None:
    site = _site("osiptel", "carrier", "lines", "total_lines")
    path = site_csv_path(tmp_path / "out.csv", site.name)
    path.write_text(
        "ruc,carrier,lines,total_lines\n20100000001,CLARO,2\n20100000002,CLARO,2,2\n",
        encoding="utf-8",
    )
    imported = import_site(store=store, output_csv=tmp_path / "out.csv", site=site)
    assert imported == 1
    assert list(store.success_rows("osiptel")) == [
        ("20100000002", (("CLARO", "2", "2"),))
    ]
    assert ("osiptel", "20100000001") not in store.done_pairs()


def test_an_invalid_ruc_row_is_skipped(store: OutcomeStore, tmp_path: Path) -> None:
    site = _site("osiptel", "carrier", "lines", "total_lines")
    path = site_csv_path(tmp_path / "out.csv", site.name)
    path.write_text(
        "ruc,carrier,lines,total_lines\nnot-a-ruc,CLARO,1,1\n", encoding="utf-8"
    )
    imported = import_site(store=store, output_csv=tmp_path / "out.csv", site=site)
    assert imported == 0


def test_rows_for_the_same_ruc_are_grouped_into_one_import(
    store: OutcomeStore, tmp_path: Path
) -> None:
    site = _site("osiptel", "carrier", "lines", "total_lines")
    path = site_csv_path(tmp_path / "out.csv", site.name)
    path.write_text(
        "ruc,carrier,lines,total_lines\n20100000001,CLARO,2,3\n20100000001,ENTEL,1,3\n",
        encoding="utf-8",
    )
    imported = import_site(store=store, output_csv=tmp_path / "out.csv", site=site)
    assert imported == 1
    assert list(store.success_rows("osiptel")) == [
        ("20100000001", (("CLARO", "2", "3"), ("ENTEL", "1", "3")))
    ]
    assert ("osiptel", "20100000001") in store.done_pairs()
