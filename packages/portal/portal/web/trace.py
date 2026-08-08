from __future__ import annotations

import ipaddress

from typing import TYPE_CHECKING

from portal.domain.models import RequestTrace


if TYPE_CHECKING:
    from litestar import Request


def client_trace(request: Request) -> RequestTrace:
    """Trust exactly one header for the client address.

    Cloudflare sets CF-Connecting-IP after stripping any client-supplied copy,
    and the origin accepts connections only from Cloudflare, so this is the
    single source for who is calling. X-Forwarded-For is deliberately not
    consulted: it is the header an attacker would forge to move themselves into
    a fresh rate-limit bucket.
    """
    return RequestTrace(
        ip=_client_ip(request),
        ray_id=request.headers.get("cf-ray"),
    )


def _client_ip(request: Request) -> str | None:
    candidate = request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else ""
    )

    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None
