from __future__ import annotations


class BrowserError(RuntimeError):
    pass


class RejectedError(BrowserError):
    """The site returned its ambiguous reject response (for Entel, HasErrorDebt).

    A reject is the one signal that drives retry classification: it is not a
    session fault but a fluctuating verdict, so the run re-mints rather than
    restarts. Keep this the single owner of that distinction.
    """
