from __future__ import annotations

import asyncio
import csv

from dataclasses import replace
from typing import TYPE_CHECKING

from fetch.cli import RunConfig
from fetch.domain.errors import BanSignalError
from fetch.domain.types import RucKind, Site
from fetch.pipeline import run as run_mod
from fetch.pipeline.run import run

from tests.fetch.conftest import FakeProvider, accepts_ruc_kinds, as_async, fake_site


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    import httpx
    import pytest

    from fetch.domain.types import Doc, Row


def _site(
    name: str,
    kinds: frozenset[RucKind],
    *,
    lookup: Callable[[httpx.AsyncClient, Doc], Awaitable[tuple[Row, ...]]],
) -> Site:
    return fake_site(
        name,
        "value",
        accepts=accepts_ruc_kinds(*kinds),
        session_budget=1,
        lookup=lookup,
    )


def _write_input(tmp_path: Path, *rucs: str) -> Path:
    path = tmp_path / "in.csv"
    path.write_text("\n".join(rucs) + "\n", encoding="utf-8")
    return path


def _cfg(
    tmp_path: Path,
    input_csv: Path,
    sites: tuple[Site, ...],
) -> RunConfig:
    return RunConfig(
        input_csv=input_csv,
        output_csv=tmp_path / "out.csv",
        sites=sites,
        dedupe=True,
        debug=False,
        session_budget=None,
        ban_cooldown_s=None,
        wait_min_s=0.0,
        wait_max_s=0.0,
        do_import=False,
    )


def _install_provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    provider = FakeProvider(workers=2)
    monkeypatch.setattr(run_mod, "load_proxy_providers", lambda: [provider])
    return provider


def test_run_writes_a_success_csv_for_every_ruc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_provider(monkeypatch)

    def lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        return (("ok",),)

    input_csv = _write_input(tmp_path, "20100000001", "20100000002")
    cfg = _cfg(
        tmp_path,
        input_csv,
        (_site("osiptel", frozenset({RucKind.JURIDICA}), lookup=as_async(lookup)),),
    )

    asyncio.run(run(cfg, run_id="r1"))

    output = tmp_path / "out.csv"
    rows = list(csv.reader(output.with_name("out.osiptel.csv").open(encoding="utf-8")))
    assert rows == [
        ["doc", "value"],
        ["20100000001", "ok"],
        ["20100000002", "ok"],
    ]


def test_run_routes_each_ruc_kind_to_the_right_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_provider(monkeypatch)
    natural_hits: list[str] = []
    juridica_hits: list[str] = []

    def natural_lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        natural_hits.append(str(doc))
        return (("N",),)

    def juridica_lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        juridica_hits.append(str(doc))
        return (("J",),)

    input_csv = _write_input(tmp_path, "10100000001", "20100000001")
    cfg = _cfg(
        tmp_path,
        input_csv,
        (
            _site(
                "natural", frozenset({RucKind.NATURAL}), lookup=as_async(natural_lookup)
            ),
            _site(
                "juridica",
                frozenset({RucKind.JURIDICA}),
                lookup=as_async(juridica_lookup),
            ),
        ),
    )

    asyncio.run(run(cfg, run_id="r"))

    assert natural_hits == ["10100000001"]
    assert juridica_hits == ["20100000001"]


def test_run_exports_failures_to_the_errors_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_provider(monkeypatch)

    def lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        msg = "blocked"
        raise BanSignalError(msg)

    input_csv = _write_input(tmp_path, "20100000001")
    cfg = _cfg(
        tmp_path,
        input_csv,
        (_site("osiptel", frozenset({RucKind.JURIDICA}), lookup=as_async(lookup)),),
    )

    asyncio.run(run(cfg, run_id="r"))

    errors_path = tmp_path / "out.osiptel.errors.csv"
    assert errors_path.exists()
    with errors_path.open(encoding="utf-8") as file_obj:
        rows = list(csv.reader(file_obj))
    assert rows[0] == [
        "doc",
        "error_code",
        "error_detail",
        "attempt",
        "session_id",
        "proxy_id",
        "provider",
        "timestamp",
    ]
    assert rows[1][0] == "20100000001"
    assert rows[1][1] == "ban_signal"


def test_run_drops_already_done_rucs_on_a_resumed_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_provider(monkeypatch)
    first_hits: list[str] = []
    second_hits: list[str] = []

    def first_lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        first_hits.append(str(doc))
        return (("ok",),)

    def second_lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        second_hits.append(str(doc))
        return (("ok",),)

    cfg = _cfg(
        tmp_path,
        _write_input(tmp_path, "20100000001", "20100000002"),
        (
            _site(
                "osiptel", frozenset({RucKind.JURIDICA}), lookup=as_async(first_lookup)
            ),
        ),
    )
    asyncio.run(run(cfg, run_id="r1"))
    assert sorted(first_hits) == ["20100000001", "20100000002"]

    # Re-run with one new Doc: the durable store is the source of truth, so only
    # the new Doc is fetched.
    cfg2 = _cfg(
        tmp_path,
        _write_input(tmp_path, "20100000001", "20100000002", "20100000003"),
        (
            _site(
                "osiptel", frozenset({RucKind.JURIDICA}), lookup=as_async(second_lookup)
            ),
        ),
    )
    asyncio.run(run(cfg2, run_id="r2"))
    assert second_hits == ["20100000003"]


def test_budget_clamps_a_cli_override_to_the_sites_own_ceiling(
    tmp_path: Path,
) -> None:
    # OSIPTEL needs a fresh session per lookup; a multi-site --session-budget
    # meant for a looser site must not relax that.
    osiptel = fake_site("osiptel", "value", session_budget=1)
    cfg = _cfg(tmp_path, tmp_path / "in.csv", (osiptel,))

    assert run_mod._budget(replace(cfg, session_budget=50), osiptel) == 1


def test_budget_honors_a_cli_override_below_the_sites_ceiling(
    tmp_path: Path,
) -> None:
    sunat = fake_site("sunat", "value", session_budget=50)
    cfg = _cfg(tmp_path, tmp_path / "in.csv", (sunat,))

    assert run_mod._budget(replace(cfg, session_budget=10), sunat) == 10


def test_budget_falls_back_to_the_sites_default_with_no_cli_override(
    tmp_path: Path,
) -> None:
    sunat = fake_site("sunat", "value", session_budget=50)
    cfg = _cfg(tmp_path, tmp_path / "in.csv", (sunat,))

    assert run_mod._budget(cfg, sunat) == 50


def test_run_releases_every_proxy_session_it_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _install_provider(monkeypatch)

    def lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
        return (("ok",),)

    cfg = _cfg(
        tmp_path,
        _write_input(tmp_path, "20100000001", "20100000002", "20100000003"),
        (_site("osiptel", frozenset({RucKind.JURIDICA}), lookup=as_async(lookup)),),
    )
    asyncio.run(run(cfg, run_id="r"))

    # Every opened session must be released exactly once.
    assert len(provider.released) == len(set(provider.released))
    assert len(provider.released) == 3
