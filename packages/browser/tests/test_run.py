from __future__ import annotations

import csv

from typing import TYPE_CHECKING, Any

import browser.run as run_module

from browser.errors import RejectedError
from browser.result import LookupResult
from browser.run import RunConfig, run
from browser.sites.base import BrowserSite
from browser.subject import Subject


if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class _FakePage:
    """Returns a scripted outcome per subject: 'reject' raises, anything else is
    the success value. The last outcome repeats once its list is drained."""

    def __init__(self, script: dict[str, list[str]]) -> None:
        self._script = script

    def lookup(self, subject: str) -> LookupResult:
        outcomes = self._script[subject]
        outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
        if outcome == "reject":
            msg = f"rejected {subject}"
            raise RejectedError(msg)
        return LookupResult(
            subject=subject, columns={"value": outcome}, elapsed_ms=1, mint_ms=1
        )


class _FakeBrowser:
    def __init__(self, **_: Any) -> None:
        pass

    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_: object) -> None:
        return None


def _site(page: _FakePage) -> BrowserSite:
    def open_page(_session: object, **_: Any) -> _FakePage:
        return page

    def row(subject: str, columns: dict[str, str], observed_at: str) -> list[str]:
        return [subject, columns["value"], observed_at]

    return BrowserSite(
        name="fake",
        url="about:blank",
        export_header=("subject", "value", "observed_at"),
        accepts=lambda _subject: True,
        open_page=open_page,
        row=row,
    )


def _config(tmp_path: Path, subjects: list[str]) -> RunConfig:
    source = tmp_path / "in.csv"
    source.write_text("\n".join(subjects) + "\n", encoding="utf-8")
    return RunConfig(
        input_csv=source,
        output_csv=tmp_path / "out.csv",
        state_db=tmp_path / "state.sqlite3",
        site="fake",
        control=None,
        software_webgl=False,
        diagnostics=None,
        max_session_restarts=0,
        use_proxy=False,
        reject_retries=3,
        reject_restart_threshold=4,
    )


def _patch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_module, "SeleniumBaseBrowser", _FakeBrowser)


def test_retries_a_reject_then_records_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    page = _FakePage({"20100000092": ["reject", "700.00"], "20100000093": ["1.00"]})
    exit_code = run(_config(tmp_path, ["20100000092", "20100000093"]), _site(page))

    assert exit_code == 0
    with (tmp_path / "out.csv").open(newline="", encoding="utf-8") as file_obj:
        rows = {row["subject"]: row["value"] for row in csv.DictReader(file_obj)}
    assert rows == {"20100000092": "700.00", "20100000093": "1.00"}


def test_exhausted_reject_budget_leaves_run_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    page = _FakePage({"20100000092": ["reject"]})
    exit_code = run(_config(tmp_path, ["20100000092"]), _site(page))

    assert exit_code == 1
    assert (tmp_path / "out.csv").read_text(encoding="utf-8").strip() == (
        "subject,value,observed_at"
    )


def test_skips_subjects_the_site_does_not_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch(monkeypatch)
    page = _FakePage({"20100000092": ["1.00"]})
    site = _site(page)
    # Only RUCs reach this site; a phone number is routed away and never looked up.
    site_ruc_only = BrowserSite(
        name=site.name,
        url=site.url,
        export_header=site.export_header,
        accepts=lambda subject: subject.kind is Subject("20100000092").kind,
        open_page=site.open_page,
        row=site.row,
    )
    exit_code = run(_config(tmp_path, ["20100000092", "987654321"]), site_ruc_only)

    assert exit_code == 0
    with (tmp_path / "out.csv").open(newline="", encoding="utf-8") as file_obj:
        rows = [row["subject"] for row in csv.DictReader(file_obj)]
    assert rows == ["20100000092"]
