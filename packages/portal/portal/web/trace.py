from __future__ import annotations

import ipaddress

from typing import TYPE_CHECKING

from portal.domain.models import RequestTrace


if TYPE_CHECKING:
    from litestar import Request


def client_trace(request: Request) -> RequestTrace:
    """CF-Connecting-IP is the one header trusted for the client address.

    Cloudflare sets it after stripping any client-supplied copy, and a
    deployed origin is reachable only through Cloudflare. Without the header,
    the socket peer is used. X-Forwarded-For is not consulted, because it is
    the header an attacker would forge to move themselves into a fresh
    rate-limit bucket.
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
