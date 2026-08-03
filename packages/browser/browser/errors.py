from __future__ import annotations


class BrowserError(RuntimeError):
    pass


class RejectedError(BrowserError):
    """The site returned its ambiguous reject response (for Entel, HasErrorDebt).

    A reject is a fluctuating verdict on a healthy session, so the run re-mints a
    token and resends. Entel fluctuates on its reCAPTCHA v3 score, portabilidad on a
    stale Turnstile token.
    """
