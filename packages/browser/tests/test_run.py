from __future__ import annotations

import csv

from contextlib import nullcontext
from typing import TYPE_CHECKING, Any

import browser.run as run_module

from browser.errors import RejectedError
from browser.result import LookupResult
from browser.run import RunConfig, run
from browser.sites.base import BrowserSite


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class _FakePage:
    """Returns a scripted outcome per RUC: 'reject' raises, anything else is the
    success value. The last outcome repeats once its list is drained."""

    def __init__(self, script: dict[str, list[str]]) -> None:
        self._script = script

    def lookup(self, ruc: str) -> LookupResult:
        outcomes = self._script[ruc]
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if outcome == "reject":
            msg = f"rejected {ruc}"
            raise RejectedError(msg)
        return LookupResult(
            ruc=ruc, columns={"value": outcome}, elapsed_ms=1, mint_ms=1
        )


class _FakeBrowser:
    def __init__(self, **_: Any) -> None:
        pass

    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_: object) -> None:
        return None


def _site(page: _FakePage) -> BrowserSite:
    def open_page(_controller: object, **_: Any) -> _FakePage:
        return page

    def row(ruc: str, columns: dict[str, str], observed_at: str) -> list[str]:
        return [ruc, columns["value"], observed_at]

    return BrowserSite(
        name="fake",
        url="about:blank",
        export_header=("ruc", "value", "observed_at"),
        open_page=open_page,
        row=row,
    )


def _config(tmp_path: Path, rucs: list[str]) -> RunConfig:
    source = tmp_path / "in.csv"
    source.write_text("\n".join(rucs) + "\n", encoding="utf-8")
    return RunConfig(
        input_csv=source,
        output_csv=tmp_path / "out.csv",
        state_db=tmp_path / "state.sqlite3",
        site="fake",
        profile=tmp_path / "profile",
        control_ruc="20610448187",
        binary=tmp_path / "chrome",
        software_webgl=False,
        diagnostics=None,
        display=None,
        max_session_restarts=0,
        reject_retries=3,
        reject_restart_threshold=4,
    )


def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_module, "DirectBrowser", _FakeBrowser)
    monkeypatch.setattr(run_module, "DedicatedDisplay", lambda _display: nullcontext())


def test_retries_a_reject_then_records_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    page = _FakePage({"20100000092": ["reject", "700.00"], "20100000093": ["1.00"]})
    exit_code = run(_config(tmp_path, ["20100000092", "20100000093"]), _site(page))

    assert exit_code == 0
    with (tmp_path / "out.csv").open(newline="", encoding="utf-8") as file_obj:
        rows = {row["ruc"]: row["value"] for row in csv.DictReader(file_obj)}
    assert rows == {"20100000092": "700.00", "20100000093": "1.00"}


def test_exhausted_reject_budget_leaves_run_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    page = _FakePage({"20100000092": ["reject"]})
    exit_code = run(_config(tmp_path, ["20100000092"]), _site(page))

    assert exit_code == 1
    assert (tmp_path / "out.csv").read_text(encoding="utf-8").strip() == (
        "ruc,value,observed_at"
    )
