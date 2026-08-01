from __future__ import annotations


class FetchError(Exception):
    pass


class TransientTransportError(FetchError):
    pass


class BanSignalError(TransientTransportError):
    pass


class ParseError(TransientTransportError):
    pass


class UpstreamNotReadyError(TransientTransportError):
    pass


class ProviderSchemaError(FetchError):
    pass


class ProxyConfigurationError(FetchError):
    """A proxy provider's configuration values are missing or out of range."""


class RucNotFoundError(FetchError):
    pass
