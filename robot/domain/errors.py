from __future__ import annotations


class RobotError(Exception):
    pass


class TransientTransportError(RobotError):
    pass


class BanSignalError(TransientTransportError):
    pass


class ParseError(TransientTransportError):
    pass


class UpstreamNotReadyError(TransientTransportError):
    pass


class ProviderSchemaError(RobotError):
    pass
