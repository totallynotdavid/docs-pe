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
    pass


class RucNotFoundError(FetchError):
    pass
