from __future__ import annotations


class BrowserError(RuntimeError):
    pass


class RejectedError(BrowserError):
    """The site returned an ambiguous rejection on an otherwise healthy session."""
