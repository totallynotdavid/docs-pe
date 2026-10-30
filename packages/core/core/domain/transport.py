from __future__ import annotations

from core.domain.errors import BanSignalError, FetchError, TransientTransportError


_TRANSIENT_STATUSES = frozenset({502, 503, 504})
_BAN_STATUSES = frozenset({403, 429})


def classify_status(status: int) -> type[FetchError] | None:
    if status in _TRANSIENT_STATUSES:
        return TransientTransportError
    if status >= 500 or status in _BAN_STATUSES:
        return BanSignalError
    if status != 200:
        return TransientTransportError
    return None
