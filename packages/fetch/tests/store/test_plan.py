from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING

from fetch.domain.types import RUC, RucKind, Site, SiteTuning
from fetch.store.plan import count_unrouted, plan_pending, read_rucs


if TYPE_CHECKING:
    from pathlib import Path

    import httpx

    from fetch.domain.types import Row


async def _ready(client: httpx.AsyncClient, site: Site) -> None:
    await asyncio.sleep(0)


async def _lookup(client: httpx.AsyncClient, ruc: RUC) -> tuple[Row, ...]:
    await asyncio.sleep(0)
    return ()


def _site(name: str, *kinds: RucKind) -> Site:
    return Site(
        name=name,
        columns=(),
        supports=frozenset(kinds),
        allows_empty=True,
        tuning=SiteTuning(session_budget=1),
        endpoints=(),
        ready=_ready,
        lookup=_lookup,
    )


def test_reads_valid_rucs_and_dedupes(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("20100000001\n20100000002\n20100000001\n", encoding="utf-8")
    rucs, counts = read_rucs(csv_path, dedupe=True)
    assert [str(r) for r in rucs] == ["20100000001", "20100000002"]
    assert counts.valid == 2
    assert counts.duplicates == 1
    assert counts.rows_read == 3


def test_keeps_duplicates_when_dedupe_is_off(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("20100000001\n20100000001\n", encoding="utf-8")
    rucs, counts = read_rucs(csv_path, dedupe=False)
    assert len(rucs) == 2
    assert counts.duplicates == 0


def test_blank_and_invalid_rows_are_ignored(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    csv_path.write_text("20100000001\n\nnot-a-ruc\n123\n", encoding="utf-8")
    rucs, counts = read_rucs(csv_path, dedupe=True)
    assert [str(r) for r in rucs] == ["20100000001"]
    assert counts.ignored == 3
    assert counts.valid == 1


def test_strips_a_utf8_bom(tmp_path: Path) -> None:
    csv_path = tmp_path / "in.csv"
    # Spreadsheet exports often carry a BOM on the first cell.
    csv_path.write_text("20100000001\n", encoding="utf-8-sig")
    rucs, counts = read_rucs(csv_path, dedupe=True)
    assert [str(r) for r in rucs] == ["20100000001"]
    assert counts.valid == 1


def test_plan_pending_excludes_already_done_pairs() -> None:
    sites = [_site("osiptel", RucKind.JURIDICA), _site("sunat", RucKind.JURIDICA)]
    rucs = [RUC("20100000001"), RUC("20100000002")]
    done = {("osiptel", "20100000001")}
    pending = plan_pending(rucs, sites, done)
    assert [str(r) for r in pending["osiptel"]] == ["20100000002"]
    assert [str(r) for r in pending["sunat"]] == ["20100000001", "20100000002"]


def test_plan_pending_routes_rucs_by_kind() -> None:
    sites = [
        _site("natural_only", RucKind.NATURAL),
        _site("juridica_only", RucKind.JURIDICA),
    ]
    rucs = [RUC("10100000001"), RUC("20100000001")]
    pending = plan_pending(rucs, sites, set())
    assert [str(r) for r in pending["natural_only"]] == ["10100000001"]
    assert [str(r) for r in pending["juridica_only"]] == ["20100000001"]


def test_plan_pending_omits_a_ruc_no_site_can_serve() -> None:
    sites = [_site("juridica_only", RucKind.JURIDICA)]
    rucs = [RUC("10100000001")]
    pending = plan_pending(rucs, sites, set())
    assert pending["juridica_only"] == []


def test_count_unrouted_counts_rucs_no_selected_site_can_serve() -> None:
    sites = [_site("juridica_only", RucKind.JURIDICA)]
    rucs = [RUC("10100000001"), RUC("20100000001"), RUC("10100000002")]
    assert count_unrouted(rucs, sites) == 2


def test_count_unrouted_is_zero_when_every_ruc_is_served() -> None:
    sites = [_site("both", RucKind.NATURAL, RucKind.JURIDICA)]
    rucs = [RUC("10100000001"), RUC("20100000001")]
    assert count_unrouted(rucs, sites) == 0
