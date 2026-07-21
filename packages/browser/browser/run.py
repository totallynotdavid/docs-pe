from __future__ import annotations

import sys

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from browser.backends.direct import DirectBrowser
from browser.diagnostics import DiagnosticLog
from browser.display import DedicatedDisplay
from browser.errors import BrowserError, RejectedError
from browser.ingest import new_run_id, read_rucs
from browser.store import ObservationStore


if TYPE_CHECKING:
    from pathlib import Path

    from browser.result import LookupResult
    from browser.ruc import RUC
    from browser.sites.base import BrowserSite, SitePage


@dataclass(frozen=True)
class RunConfig:
    input_csv: Path
    output_csv: Path
    state_db: Path
    site: str
    profile: Path
    control_ruc: str
    binary: Path
    software_webgl: bool
    diagnostics: Path | None
    display: int | None
    max_session_restarts: int
    reject_retries: int = 12
    reject_restart_threshold: int = 4


def run(config: RunConfig, site: BrowserSite) -> int:
    rucs, counts = read_rucs(config.input_csv, dedupe=True)
    if not rucs:
        print("No valid RUCs found in input.", file=sys.stderr)
        return 2

    run_id = new_run_id()
    diagnostic_log = _make_log(config, run_id=run_id, site=site)
    print(
        f"{site.name} run {run_id}: {len(rucs)} RUCs "
        f"({counts.ignored} invalid/blank rows ignored)"
    )

    with ObservationStore(config.state_db) as store:
        driver = _Driver(
            config=config,
            site=site,
            store=store,
            run_id=run_id,
            diagnostic_log=diagnostic_log,
        )
        try:
            with DedicatedDisplay(config.display):
                driver.run_all(rucs)
        finally:
            store.export_current(
                config.output_csv,
                site=site.name,
                header=site.export_header,
                project=site.row,
            )
            observations = store.observation_count()

    remaining = len(rucs) - driver.index
    print(
        f"Summary: {driver.succeeded} ok, {driver.rejected} rejected, "
        f"{driver.failed} failed, {remaining} remaining, "
        f"{observations} tracked observations"
    )
    return 0 if remaining == 0 and not driver.unresolved else 1


@dataclass
class _Driver:
    """Owns the automation policy: reject-retry per RUC and session restart across
    RUCs. Holds the run-wide counters so each step stays small and linear."""

    config: RunConfig
    site: BrowserSite
    store: ObservationStore
    run_id: str
    diagnostic_log: DiagnosticLog | None
    index: int = 0
    succeeded: int = 0
    rejected: int = 0
    failed: int = 0
    unresolved: set[str] = field(default_factory=set)

    def run_all(self, rucs: list[RUC]) -> None:
        restarts = 0
        while self.index < len(rucs):
            try:
                self._drive_session(rucs)
            except BrowserError as exc:
                if restarts >= self.config.max_session_restarts:
                    print(
                        f"{self.site.name} session stopped after "
                        f"{restarts} restart(s): {exc}",
                        file=sys.stderr,
                    )
                    return
                restarts += 1
                print(f"Restarting {self.site.name} session ({restarts}): {exc}")

    def _drive_session(self, rucs: list[RUC]) -> None:
        with DirectBrowser(
            binary=self.config.binary,
            profile=self.config.profile,
            software_webgl=self.config.software_webgl,
            url=self.site.url,
        ) as controller:
            page = self.site.open_page(
                controller,
                control_ruc=self.config.control_ruc,
                reset_cookies=False,
                diagnostic_log=self.diagnostic_log,
            )
            consecutive_rejects = 0
            while self.index < len(rucs):
                consecutive_rejects = self._process(
                    page, str(rucs[self.index]), consecutive_rejects
                )

    def _process(self, page: SitePage, ruc: str, consecutive_rejects: int) -> int:
        previous = self.store.latest(self.site.name, ruc)
        try:
            result = _lookup_with_retries(page, ruc, retries=self.config.reject_retries)
        except RejectedError as exc:
            return self._on_reject(ruc, consecutive_rejects, exc)
        except BrowserError as exc:
            self._on_failure(ruc, exc)
            raise
        self._on_success(ruc, result, previous)
        return 0

    def _on_success(
        self, ruc: str, result: LookupResult, previous: dict[str, str] | None
    ) -> None:
        self.store.record_success(
            run_id=self.run_id, site=self.site.name, ruc=ruc, columns=result.columns
        )
        changed = previous is not None and previous != result.columns
        marker = " CHANGED" if changed else ""
        print(f"OK {ruc} {_summary(result.columns)} ({result.elapsed_ms} ms){marker}")
        self.succeeded += 1
        self.unresolved.discard(ruc)
        self.index += 1

    def _on_reject(self, ruc: str, consecutive_rejects: int, exc: RejectedError) -> int:
        self.store.record_failure(
            run_id=self.run_id,
            site=self.site.name,
            ruc=ruc,
            status="rejected",
            error_detail=str(exc),
        )
        self.rejected += 1
        self.unresolved.add(ruc)
        self.index += 1
        consecutive_rejects += 1
        print(f"REJECTED {ruc} after {self.config.reject_retries + 1} mints")
        if consecutive_rejects >= self.config.reject_restart_threshold:
            msg = (
                f"{consecutive_rejects} RUCs in a row exhausted retries; "
                "restarting session"
            )
            raise BrowserError(msg)
        return consecutive_rejects

    def _on_failure(self, ruc: str, exc: BrowserError) -> None:
        self.store.record_failure(
            run_id=self.run_id,
            site=self.site.name,
            ruc=ruc,
            status="failed",
            error_detail=str(exc),
        )
        self.failed += 1
        self.unresolved.add(ruc)


def _lookup_with_retries(page: SitePage, ruc: str, *, retries: int) -> LookupResult:
    # The verdict is a fluctuating reCAPTCHA v3 score, so a single mint clears
    # only about half the time. Re-mint a fresh token before giving up; a hard
    # BrowserError is not a reject and propagates immediately.
    last_reject: RejectedError | None = None
    for _ in range(retries + 1):
        try:
            return page.lookup(ruc)
        except RejectedError as exc:
            last_reject = exc
    assert last_reject is not None  # range(retries + 1) runs at least once
    raise last_reject


def _make_log(
    config: RunConfig, *, run_id: str, site: BrowserSite
) -> DiagnosticLog | None:
    if config.diagnostics is None:
        return None
    log = DiagnosticLog(config.diagnostics, run_id=run_id, source="direct-chrome")
    log.record(
        {
            "stage": "launcher",
            "site": site.name,
            "binary": str(config.binary),
            "softwareWebgl": config.software_webgl,
        }
    )
    return log


def _summary(columns: dict[str, str]) -> str:
    return " ".join(f"{name}={value}" for name, value in columns.items())
