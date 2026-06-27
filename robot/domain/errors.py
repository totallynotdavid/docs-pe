from __future__ import annotations


class RobotError(Exception):
    pass


class TransientTransportError(RobotError):
    """Temporary network or upstream failures."""


class BanSignalError(TransientTransportError):
    """Signal that proxy or session is temporarily blocked."""


class ParseError(TransientTransportError):
    """Response format is not parseable or empty."""


class ProviderSchemaError(RobotError):
    """Provider response shape changed or violated its expected contract."""


class PermanentInputError(RobotError):
    """Invalid input that should not be retried."""
