from __future__ import annotations

import asyncio
import csv

from typing import TYPE_CHECKING

import pytest

from fetch.cli import RunConfig
from fetch.domain.errors import BanSignalError
from fetch.domain.types import RUC, RucKind, Site, SiteTuning
from fetch.pipeline import run as run_mod
from fetch.pipeline import session as session_mod
from fetch.pipeline.run import run
from fetch.proxy.base import ProviderTuning, ProxySession


if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    import httpx

    from fetch.domain.types import Row


class _FakeProvider:
    name = "fake"
    tuning = ProviderTuning(workers=2, ban_cooldown_s=0.0)

    def __init__(self) -> None:
        self._n = 0
        self.released: list[str] = []

    def new_session(self, *, slot_id: int) -> ProxySession:
        self._n += 1
        return ProxySession(
            proxy_id=f"proxy-{self._n}",
            host="proxy.test",
            port="9999",
            username="u",
            password="p",
            session_id=f"sess-{self._n}",
        )

    async def release(self, session: ProxySession) -> None:
        self.released.append(session.proxy_id)


def _site(
    name: str,
    supports: frozenset[RucKind],
    *,
    lookup: Callable[[httpx.AsyncClient, RUC], Awaitable[tuple[Row, ...]]],
) -> Site:
    async def ready(client: httpx.AsyncClient, site: Site) -> None:  # noqa: RUF029
        return None

    return Site(
        name=name,
        columns=("value",),
        supports=supports,
        allows_empty=True,
        tuning=SiteTuning(session_budget=1),
        endpoints=(),
        ready=ready,
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
    *,
    workers: int | None = None,
) -> RunConfig:
    return RunConfig(
        input_csv=input_csv,
        output_csv=tmp_path / "out.csv",
        sites=sites,
        dedupe=True,
        debug=False,
        session_budget=None,
        workers=workers,
        ban_cooldown_s=None,
        wait_min_s=0.0,
        wait_max_s=0.0,
        env_file="/nonexistent/does-not-exist.env",
        do_import=False,
    )


@pytest.fixture(autouse=True)
def _no_real_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve(proxy: ProxySession) -> str:  # noqa: RUF029
        return "1.2.3.4"

    monkeypatch.setattr(session_mod, "resolve_egress_ip", fake_resolve)


def _install_provider(monkeypatch: pytest.MonkeyPatch) -> _FakeProvider:
    provider = _FakeProvider()
    monkeypatch.setattr(run_mod, "load_proxy_providers", lambda *, env_file: [provider])
    return provider


def test_run_writes_a_success_csv_for_every_ruc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_provider(monkeypatch)

    async def lookup(client: httpx.AsyncClient, ruc: RUC) -> tuple[Row, ...]:  # noqa: RUF029
        return (("ok",),)

    input_csv = _write_input(tmp_path, "20100000001", "20100000002")
    cfg = _cfg(
        tmp_path,
        input_csv,
        (_site("osiptel", frozenset({RucKind.JURIDICA}), lookup=lookup),),
    )

    asyncio.run(run(cfg, run_id="r1"))

    output = tmp_path / "out.csv"
    rows = list(csv.reader(output.with_name("out.osiptel.csv").open(encoding="utf-8")))
    assert rows == [
        ["ruc", "value"],
        ["20100000001", "ok"],
        ["20100000002", "ok"],
    ]


def test_run_routes_each_ruc_kind_to_the_right_site(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_provider(monkeypatch)
    natural_hits: list[str] = []
    juridica_hits: list[str] = []

    async def natural_lookup(client: httpx.AsyncClient, ruc: RUC) -> tuple[Row, ...]:  # noqa: RUF029
        natural_hits.append(str(ruc))
        return (("N",),)

    async def juridica_lookup(client: httpx.AsyncClient, ruc: RUC) -> tuple[Row, ...]:  # noqa: RUF029
        juridica_hits.append(str(ruc))
        return (("J",),)

    input_csv = _write_input(tmp_path, "10100000001", "20100000001")
    cfg = _cfg(
        tmp_path,
        input_csv,
        (
            _site("natural", frozenset({RucKind.NATURAL}), lookup=natural_lookup),
            _site("juridica", frozenset({RucKind.JURIDICA}), lookup=juridica_lookup),
        ),
    )

    asyncio.run(run(cfg, run_id="r"))

    assert natural_hits == ["10100000001"]
    assert juridica_hits == ["20100000001"]


def test_run_exports_failures_to_the_errors_csv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_provider(monkeypatch)

    async def lookup(client: httpx.AsyncClient, ruc: RUC) -> tuple[Row, ...]:  # noqa: RUF029
        msg = "blocked"
        raise BanSignalError(msg)

    input_csv = _write_input(tmp_path, "20100000001")
    cfg = _cfg(
        tmp_path,
        input_csv,
        (_site("osiptel", frozenset({RucKind.JURIDICA}), lookup=lookup),),
    )

    asyncio.run(run(cfg, run_id="r"))

    errors_path = tmp_path / "out.osiptel.errors.csv"
    assert errors_path.exists()
    with errors_path.open(encoding="utf-8") as file_obj:
        rows = list(csv.reader(file_obj))
    assert rows[0] == [
        "ruc",
        "error_code",
        "error_detail",
        "attempt",
        "session_id",
        "proxy_id",
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

    async def first_lookup(client: httpx.AsyncClient, ruc: RUC) -> tuple[Row, ...]:  # noqa: RUF029
        first_hits.append(str(ruc))
        return (("ok",),)

    async def second_lookup(client: httpx.AsyncClient, ruc: RUC) -> tuple[Row, ...]:  # noqa: RUF029
        second_hits.append(str(ruc))
        return (("ok",),)

    cfg = _cfg(
        tmp_path,
        _write_input(tmp_path, "20100000001", "20100000002"),
        (_site("osiptel", frozenset({RucKind.JURIDICA}), lookup=first_lookup),),
    )
    asyncio.run(run(cfg, run_id="r1"))
    assert sorted(first_hits) == ["20100000001", "20100000002"]

    # Re-run with one new RUC: the durable store is the source of truth, so only
    # the new RUC is fetched.
    cfg2 = _cfg(
        tmp_path,
        _write_input(tmp_path, "20100000001", "20100000002", "20100000003"),
        (_site("osiptel", frozenset({RucKind.JURIDICA}), lookup=second_lookup),),
    )
    asyncio.run(run(cfg2, run_id="r2"))
    assert second_hits == ["20100000003"]


def test_run_releases_every_proxy_session_it_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = _install_provider(monkeypatch)

    async def lookup(client: httpx.AsyncClient, ruc: RUC) -> tuple[Row, ...]:  # noqa: RUF029
        return (("ok",),)

    cfg = _cfg(
        tmp_path,
        _write_input(tmp_path, "20100000001", "20100000002", "20100000003"),
        (_site("osiptel", frozenset({RucKind.JURIDICA}), lookup=lookup),),
    )
    asyncio.run(run(cfg, run_id="r"))

    # Every opened session must be released exactly once.
    assert len(provider.released) == len(set(provider.released))
    assert len(provider.released) == 3
