from __future__ import annotations

from typing import TYPE_CHECKING

from core.domain.errors import BanSignalError, FetchError, TransientTransportError


if TYPE_CHECKING:
    from collections.abc import Iterable

    import httpx

    from core.domain.types import Endpoint


# The one owner of "which HTTP status means what" across every site. A site wanting
# its own diagnostic message calls classify_status; everyone else calls
# raise_for_status.
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


def raise_for_status(status: int, *, endpoint: Endpoint) -> None:
    fault = classify_status(status)
    if fault is None:
        return
    word = "transient" if status in _TRANSIENT_STATUSES else "failed"
    msg = f"{endpoint.name} {word} status={status}"
    raise fault(msg)


async def warm_endpoints(
    client: httpx.AsyncClient, endpoints: Iterable[Endpoint]
) -> None:
    """A GET-and-classify check per endpoint.

    Enough for a host that needs only a status check. A site that needs cookies
    or WAF-text detection (OSIPTEL) writes its own ready() instead.
    """
    for endpoint in endpoints:
        response = await client.get(endpoint.url)
        raise_for_status(response.status_code, endpoint=endpoint)
