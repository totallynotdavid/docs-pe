from __future__ import annotations


class CaptureError(RuntimeError):
    pass


class RejectedError(CaptureError):
    """The site returned its ambiguous reject response (for Entel, HasErrorDebt).

    A reject is not a fault; it is the site declining an otherwise well-formed
    lookup. Keep this the single owner of that distinction.
    """
