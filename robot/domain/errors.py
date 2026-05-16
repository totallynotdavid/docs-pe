from __future__ import annotations


class RobotError(Exception):
    """Base class for expected runtime errors."""


class TransientTransportError(RobotError):
    """Temporary network or upstream failures."""


class BanSignalError(TransientTransportError):
    """Signal that proxy or session is temporarily blocked."""


class CaptchaError(RobotError):
    """Captcha token generation or validation failures."""


class CaptchaTimeoutError(CaptchaError):
    """Captcha token generation exceeded deadline."""


class ParseError(TransientTransportError):
    """Response format is not parseable or empty."""


class PermanentInputError(RobotError):
    """Invalid input that should not be retried."""
