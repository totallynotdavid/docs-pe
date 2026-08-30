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

# The checkbox lives in a closed shadow root, so only a GUI click can reach it.
TOKEN_JS = (
    "(() => { try { return window.turnstile.getResponse() || ''; }"
    " catch (e) { return ''; } })()"
)

FORM_SETTLE_S = 3.0


class PortabilidadPage:
    def __init__(
        self,
        *,
        session: Session,
        diagnostic_log: DiagnosticLog | None = None,
    ) -> None:
        self._session = session
        self._diagnostic_log = diagnostic_log

    def prepare(self) -> None:
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

        page_source = self._session.get_page_source()
        columns = parse_result(page_source, expected_number=number)
        elapsed_ms = int((time.monotonic() - started) * 1000)

        return LookupResult(
            subject=number,
            columns=columns,
            elapsed_ms=elapsed_ms,
            mint_ms=mint_ms,
        )

    def _solve_turnstile(
        self,
        *,
        attempts: int = 10,
        poll_seconds: int = 12,
    ) -> int:
        started = time.monotonic()

        for _ in range(attempts):
            # The widget may not be interactive immediately after the form settles.
            self._session.gui_click_captcha()

            for _ in range(poll_seconds):
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
