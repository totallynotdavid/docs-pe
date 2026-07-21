from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from fetch.domain.types import Doc, DocKind, RucKind, Site, SiteTuning
from fetch.store.plan import count_unrouted, plan_pending, read_docs


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import httpx

    from fetch.domain.types import Row


async def _ready(client: httpx.AsyncClient, site: Site) -> None:
    await asyncio.sleep(0)


async def _lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
    await asyncio.sleep(0)
    return ()


def _accepts_ruc_kinds(*kinds: RucKind) -> Callable[[Doc], bool]:
    def accepts(doc: Doc) -> bool:
        return doc.kind is DocKind.RUC and doc.ruc_kind in kinds

    return accepts


def _accepts_any(doc: Doc) -> bool:
    return True


def _site(name: str, accepts: Callable[[Doc], bool]) -> Site:
    return Site(
        name=name,
        columns=(),
        accepts=accepts,
        allows_empty=True,
        tuning=SiteTuning(session_budget=1),
        endpoints=(),
        ready=_ready,
        lookup=_lookup,
    )


def test_reads_valid_docs_and_dedupes(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("20100000001\n20100000002\n20100000001\n", encoding="utf-8")
    docs, counts = read_docs(csv_path, dedupe=True)
    assert [str(d) for d in docs] == ["20100000001", "20100000002"]
    assert counts.valid == 2
    assert counts.duplicates == 1
    assert counts.rows_read == 3


def test_reads_a_mixed_dni_and_ruc_input(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("42953322\n20100000001\n", encoding="utf-8")
    docs, counts = read_docs(csv_path, dedupe=True)
    assert [(str(d), d.kind) for d in docs] == [
        ("42953322", DocKind.DNI),
        ("20100000001", DocKind.RUC),
    ]
    assert counts.valid == 2


def test_a_seven_digit_dni_dedupes_against_its_padded_form(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("2953322\n02953322\n", encoding="utf-8")
    docs, counts = read_docs(csv_path, dedupe=True)
    assert [str(d) for d in docs] == ["02953322"]
    assert counts.duplicates == 1


def test_keeps_duplicates_when_dedupe_is_off(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("20100000001\n20100000001\n", encoding="utf-8")
    docs, counts = read_docs(csv_path, dedupe=False)
    assert len(docs) == 2
    assert counts.duplicates == 0


def test_blank_and_invalid_rows_are_ignored(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("20100000001\n\nnot-a-doc\n123\n", encoding="utf-8")
    docs, counts = read_docs(csv_path, dedupe=True)
    assert [str(d) for d in docs] == ["20100000001"]
    assert counts.ignored == 3
    assert counts.valid == 1


def test_strips_a_utf8_bom(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    # Spreadsheet exports often carry a BOM on the first cell.
    csv_path.write_text("20100000001\n", encoding="utf-8-sig")
    docs, counts = read_docs(csv_path, dedupe=True)
    assert [str(d) for d in docs] == ["20100000001"]
    assert counts.valid == 1


def test_plan_pending_excludes_already_done_pairs() -> None:
    sites = [
        _site("osiptel", _accepts_any),
        _site("sunat", _accepts_ruc_kinds(RucKind.JURIDICA)),
    ]
    docs = [Doc("20100000001"), Doc("20100000002")]
    done = {("osiptel", "20100000001")}
    pending = plan_pending(docs, sites, done)
    assert [str(d) for d in pending["osiptel"]] == ["20100000002"]
    assert [str(d) for d in pending["sunat"]] == ["20100000001", "20100000002"]


def test_plan_pending_routes_docs_by_kind() -> None:
    sites = [
        _site("natural_only", _accepts_ruc_kinds(RucKind.NATURAL)),
        _site("juridica_only", _accepts_ruc_kinds(RucKind.JURIDICA)),
        _site("osiptel", _accepts_any),
    ]
    docs = [Doc("10100000001"), Doc("20100000001"), Doc("42953322")]
    pending = plan_pending(docs, sites, set())
    assert [str(d) for d in pending["natural_only"]] == ["10100000001"]
    assert [str(d) for d in pending["juridica_only"]] == ["20100000001"]
    # OSIPTEL serves every document, DNI included.
    assert [str(d) for d in pending["osiptel"]] == [
        "10100000001",
        "20100000001",
        "42953322",
    ]


def test_plan_pending_omits_a_doc_no_site_can_serve() -> None:
    sites = [_site("juridica_only", _accepts_ruc_kinds(RucKind.JURIDICA))]
    docs = [Doc("10100000001"), Doc("42953322")]
    pending = plan_pending(docs, sites, set())
    assert pending["juridica_only"] == []


def test_count_unrouted_counts_docs_no_selected_site_can_serve() -> None:
    sites = [_site("juridica_only", _accepts_ruc_kinds(RucKind.JURIDICA))]
    docs = [Doc("10100000001"), Doc("20100000001"), Doc("42953322")]
    assert count_unrouted(docs, sites) == 2


def test_count_unrouted_is_zero_when_every_doc_is_served() -> None:
    sites = [_site("osiptel", _accepts_any)]
    docs = [Doc("10100000001"), Doc("20100000001"), Doc("42953322")]
    assert count_unrouted(docs, sites) == 0
