from __future__ import annotations

import time

from typing import TYPE_CHECKING

from browser.errors import BrowserError, RejectedError
from browser.result import LookupResult
from browser.sites.portabilidad.parse import (
    CAPTCHA_ERROR,
    RESULT_MARKER,
    parse_result,
)


if TYPE_CHECKING:
    from browser.diagnostics import DiagnosticLog
    from browser.session import Session


URL = "https://consulta.portabilidad.pe/"
NUMBER_INPUT = "#hf-number"
SUBMIT_BUTTON = 'button[formaction="/?handler=Check"]'
# Turnstile mints the token into window.turnstile.getResponse(); the checkbox lives
# in a closed shadow root, so only a GUI (screen-coordinate) click lands on it.
TOKEN_JS = (
    "(() => { try { return window.turnstile.getResponse() || ''; }"
    " catch (e) { return ''; } })()"
)
FORM_SETTLE_S = 3.0


class PortabilidadPage:
    """Drives consulta.portabilidad.pe: fill the number, clear Turnstile, submit,
    parse the returned card. Each lookup re-navigates, because both the antiforgery
    token and the Turnstile token are single use."""

    def __init__(
        self, *, session: Session, diagnostic_log: DiagnosticLog | None = None
    ) -> None:
        self._session = session
        self._diagnostic_log = diagnostic_log

    def prepare(self) -> None:
        # A health check at session open: prove the form reaches us before the first
        # lookup, so a broken site fails at startup rather than mid-run.
        self._session.goto(URL)
        self._wait_for_form()

    def lookup(self, number: str, *, timeout_s: float = 30.0) -> LookupResult:
        started = time.monotonic()
        self._session.goto(URL)
        self._wait_for_form()
        self._session.sleep(FORM_SETTLE_S)
        self._session.type(NUMBER_INPUT, number)
        mint_ms = self._solve_turnstile()
        self._session.click(SUBMIT_BUTTON)
        self._await_result(timeout_s=timeout_s)
        columns = parse_result(self._session.get_page_source(), expected_number=number)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return LookupResult(
            subject=number, columns=columns, elapsed_ms=elapsed_ms, mint_ms=mint_ms
        )

    def _solve_turnstile(self, *, attempts: int = 10, poll_s: int = 12) -> int:
        started = time.monotonic()
        for _ in range(attempts):
            # The first click reliably misses: the widget is not interactive the
            # instant the form settles. A landed click mints within a few seconds, so
            # poll briefly and re-click, which is idempotent. Waiting out one long
            # window instead pins every solve near 48s.
            self._session.gui_click_captcha()
            for _ in range(poll_s):
                if self._session.evaluate(TOKEN_JS):
                    return int((time.monotonic() - started) * 1000)
                self._session.sleep(1)
        msg = "portabilidad Turnstile token was never minted"
        raise BrowserError(msg)

    def _await_result(self, *, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            body = self._session.get_text("body")
            if RESULT_MARKER in body:
                return
            if CAPTCHA_ERROR in body:
                msg = f"portabilidad rejected the Turnstile token: {CAPTCHA_ERROR}"
                raise RejectedError(msg)
            self._session.sleep(0.5)
        msg = "portabilidad result did not render"
        raise BrowserError(msg)

    def _wait_for_form(self, *, timeout_s: float = 45.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._session.is_element_present(NUMBER_INPUT):
                return
            self._session.sleep(0.5)
        msg = "portabilidad form did not render"
        raise BrowserError(msg)
