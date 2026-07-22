from __future__ import annotations

import sys

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from browser.backends.seleniumbase import SeleniumBaseBrowser
from browser.diagnostics import DiagnosticLog
from browser.errors import BrowserError, RejectedError
from browser.ingest import new_run_id, read_subjects
from browser.proxy import load_proxy_provider
from browser.store import ObservationStore


if TYPE_CHECKING:
    from pathlib import Path

    from browser.proxy import ProxyProvider
    from browser.result import LookupResult
    from browser.sites.base import BrowserSite, SitePage
    from browser.subject import Subject


@dataclass(frozen=True)
class RunConfig:
    input_csv: Path
    output_csv: Path
    state_db: Path
    site: str
    control: str | None
    software_webgl: bool
    diagnostics: Path | None
    max_session_restarts: int
    env_file: str = ".env"
    use_proxy: bool = True
    reject_retries: int = 12
    reject_restart_threshold: int = 4


def run(config: RunConfig, site: BrowserSite) -> int:
    # Resolve the proxy first so a misconfigured .env fails before any work.
    provider = (
        load_proxy_provider(env_file=config.env_file) if config.use_proxy else None
    )

    subjects, counts = read_subjects(config.input_csv, dedupe=True)
    if not subjects:
        print("No valid subjects found in input.", file=sys.stderr)
        return 2

    run_id = new_run_id()
    diagnostic_log = _make_log(config, run_id=run_id, site=site, provider=provider)
    print(f"proxy: {provider.name} (Peru exit)" if provider else "proxy: disabled")

    with ObservationStore(config.state_db) as store:
        routed = [subject for subject in subjects if site.accepts(subject)]
        unrouted = len(subjects) - len(routed)
        done = store.done_subjects(site.name)
        pending = [subject for subject in routed if str(subject) not in done]
        skipped = len(routed) - len(pending)
        print(
            f"{site.name} run {run_id}: {len(pending)} pending "
            f"({counts.ignored} invalid/blank rows ignored, {unrouted} not served by "
            f"{site.name}, {skipped} already done)"
        )

        driver = _Driver(
            config=config,
            site=site,
            store=store,
            run_id=run_id,
            diagnostic_log=diagnostic_log,
            provider=provider,
        )
        try:
            driver.run_all(pending)
        finally:
            store.export_current(
                config.output_csv,
                site=site.name,
                header=site.export_header,
                project=site.row,
            )
            observations = store.observation_count()

    remaining = len(pending) - driver.index
    print(
        f"Summary: {driver.succeeded} ok, {driver.rejected} rejected, "
        f"{driver.failed} failed, {remaining} remaining, "
        f"{observations} tracked observations"
    )
    return 0 if remaining == 0 and not driver.unresolved else 1


@dataclass
class _Driver:
    """Owns the automation policy: reject-retry per subject and session restart
    across subjects. Holds the run-wide counters so each step stays small and
    linear."""

    config: RunConfig
    site: BrowserSite
    store: ObservationStore
    run_id: str
    diagnostic_log: DiagnosticLog | None
    provider: ProxyProvider | None
    index: int = 0
    succeeded: int = 0
    rejected: int = 0
    failed: int = 0
    unresolved: set[str] = field(default_factory=set)

    def run_all(self, subjects: list[Subject]) -> None:
        restarts = 0
        while self.index < len(subjects):
            try:
                self._drive_session(subjects)
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

    def _drive_session(self, subjects: list[Subject]) -> None:
        # A fresh endpoint per session means each restart (a ban) rotates to a
        # new exit IP; None routes direct.
        proxy = (
            self.provider.new_endpoint().as_chrome_proxy() if self.provider else None
        )
        with SeleniumBaseBrowser(
            url=self.site.url,
            software_webgl=self.config.software_webgl,
            proxy=proxy,
        ) as session:
            page = self.site.open_page(
                session,
                control=self.config.control,
                reset_cookies=False,
                diagnostic_log=self.diagnostic_log,
            )
            consecutive_rejects = 0
            while self.index < len(subjects):
                consecutive_rejects = self._process(
                    page, str(subjects[self.index]), consecutive_rejects
                )

    def _process(self, page: SitePage, subject: str, consecutive_rejects: int) -> int:
        previous = self.store.latest(self.site.name, subject)
        try:
            result = _lookup_with_retries(
                page, subject, retries=self.config.reject_retries
            )
        except RejectedError as exc:
            return self._on_reject(subject, consecutive_rejects, exc)
        except BrowserError as exc:
            self._on_failure(subject, exc)
            raise
        self._on_success(subject, result, previous)
        return 0

    def _on_success(
        self, subject: str, result: LookupResult, previous: dict[str, str] | None
    ) -> None:
        self.store.record_success(
            run_id=self.run_id,
            site=self.site.name,
            subject=subject,
            columns=result.columns,
        )
        changed = previous is not None and previous != result.columns
        marker = " CHANGED" if changed else ""
        print(
            f"OK {subject} {_summary(result.columns)} ({result.elapsed_ms} ms){marker}"
        )
        self.succeeded += 1
        self.unresolved.discard(subject)
        self.index += 1

    def _on_reject(
        self, subject: str, consecutive_rejects: int, exc: RejectedError
    ) -> int:
        self.store.record_failure(
            run_id=self.run_id,
            site=self.site.name,
            subject=subject,
            status="rejected",
            error_detail=str(exc),
        )
        self.rejected += 1
        self.unresolved.add(subject)
        self.index += 1
        consecutive_rejects += 1
        print(f"REJECTED {subject} after {self.config.reject_retries + 1} mints")
        if consecutive_rejects >= self.config.reject_restart_threshold:
            msg = (
                f"{consecutive_rejects} subjects in a row exhausted retries; "
                "restarting session"
            )
            raise BrowserError(msg)
        return consecutive_rejects

    def _on_failure(self, subject: str, exc: BrowserError) -> None:
        self.store.record_failure(
            run_id=self.run_id,
            site=self.site.name,
            subject=subject,
            status="failed",
            error_detail=str(exc),
        )
        self.failed += 1
        self.unresolved.add(subject)


def _lookup_with_retries(page: SitePage, subject: str, *, retries: int) -> LookupResult:
    # A reject is a fluctuating verdict (Entel's reCAPTCHA v3 score, portabilidad's
    # stale Turnstile token), so re-mint a fresh token before giving up; a hard
    # BrowserError is not a reject and propagates immediately.
    last_reject: RejectedError | None = None
    for _ in range(retries + 1):
        try:
            return page.lookup(subject)
        except RejectedError as exc:
            last_reject = exc
    assert last_reject is not None  # range(retries + 1) runs at least once
    raise last_reject


def _make_log(
    config: RunConfig,
    *,
    run_id: str,
    site: BrowserSite,
    provider: ProxyProvider | None,
) -> DiagnosticLog | None:
    if config.diagnostics is None:
        return None
    log = DiagnosticLog(config.diagnostics, run_id=run_id, source="seleniumbase-cdp")
    log.record(
        {
            "stage": "launcher",
            "site": site.name,
            "softwareWebgl": config.software_webgl,
            # Provider name only; the endpoint string carries the password.
            "proxy": provider.name if provider else None,
        }
    )
    return log


def _summary(columns: dict[str, str]) -> str:
    return " ".join(f"{name}={value}" for name, value in columns.items())
