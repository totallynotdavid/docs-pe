from __future__ import annotations

import asyncio
import logging
import time

from typing import TYPE_CHECKING, Any

import httpx

from robot.domain.errors import (
    BanSignalError,
    ParseError,
    TransientTransportError,
    UpstreamNotReadyError,
)
from robot.domain.transport import classify_status
from robot.domain.types import Endpoint, RucKind, Site, SiteTuning
from robot.obs.events import FETCH_PAGE_OK, FETCH_PAGE_START, SITE_REQUEST_FAILED
from robot.obs.logging import kv
from robot.sites.osiptel.parser import parse_page
from robot.sites.osiptel.payload import PageRequest, build_payload


if TYPE_CHECKING:
    from robot.domain.types import RUC, Row


logger = logging.getLogger(__name__)

HOME_URL = "https://checatuslineas.osiptel.gob.pe/"
API_URL = "https://checatuslineas.osiptel.gob.pe/Consultas/GetAllCabeceraConsulta/"
# OSIPTEL caps each page at 5000 rows; the loop in _lookup pages past the cap.
PAGE_SIZE = 5000
_READY_TIMEOUT_S = 25.0
_READY_POLL_S = 0.25

_HOME = Endpoint(name="home", url=HOME_URL, warm=True)
_API = Endpoint(name="api", url=API_URL)


async def _ready(client: httpx.AsyncClient, site: Site) -> None:
    # Poll the home page to seed cookies (kept in the client jar and auto-sent on the
    # API POST) and to detect a WAF block on this exit before spending a lookup.
    home = next(endpoint for endpoint in site.endpoints if endpoint.warm)
    deadline = time.monotonic() + _READY_TIMEOUT_S
    last_status = 0
    last_body = ""
    last_error = ""
    while time.monotonic() < deadline:
        try:
            response = await client.get(home.url)
        # follow_redirects raises httpx.TooManyRedirects at the client level, past
        # the transport that would otherwise normalize it, so catch both.
        except (TransientTransportError, httpx.HTTPError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(_READY_POLL_S)
            continue

        last_status = response.status_code
        last_body = response.text.replace("\n", " ").strip()[:240]
        # OSIPTEL's WAF returns 200 with a CAPTCHA wall rather than a 4xx, so the
        # success marker string is the real readiness signal.
        if response.status_code == 200 and "Checa tus l" in response.text:
            return
        if _is_waf_block(response.text):
            msg = f"osiptel page blocked status={response.status_code} body={last_body}"
            raise BanSignalError(msg)
        await asyncio.sleep(_READY_POLL_S)

    msg = (
        "osiptel page not ready "
        f"status={last_status} error={last_error} body={last_body}"
    )
    raise UpstreamNotReadyError(msg)


async def _lookup(client: httpx.AsyncClient, ruc: RUC) -> tuple[Row, ...]:
    total: int | None = None
    start = 0
    draw = 1
    counts: dict[str, int] = {}

    while True:
        payload = await _fetch_page(
            client, ruc=str(ruc), draw=draw, start=start, length=PAGE_SIZE
        )
        page = parse_page(payload)
        total = page.total_records

        for carrier, lines in page.carrier_counts.items():
            counts[carrier] = counts.get(carrier, 0) + lines

        if total == 0 or page.rows_returned == 0:
            break
        start += page.rows_returned
        draw += 1
        if start >= total:
            break

    grand_total = total or 0
    return tuple(
        (carrier, lines, grand_total) for carrier, lines in sorted(counts.items())
    )


async def _fetch_page(
    client: httpx.AsyncClient, *, ruc: str, draw: int, start: int, length: int
) -> Any:
    started = time.perf_counter()
    logger.info(
        "%s %s",
        FETCH_PAGE_START,
        kv(ruc=ruc, draw=draw, start=start, length=length),
    )
    data = build_payload(PageRequest(ruc=ruc, draw=draw, start=start, length=length))
    # Transport faults are normalized to TransientTransportError by the client
    # transport (proxy/transport.py), so there is nothing to catch here.
    response = await client.post(_API.url, data=data, headers=_api_headers())

    status = response.status_code
    fault = classify_status(status)
    if fault is not None:
        body = _log_failed(
            status, ruc=ruc, draw=draw, start=start, length=length, body=response.text
        )
        if fault is BanSignalError:
            msg = (
                "osiptel request failed "
                f"status={status} draw={draw} start={start} length={length} "
                f"ruc={ruc} body={body}"
            )
        else:
            msg = f"osiptel request transient status={status}"
        raise fault(msg)

    try:
        payload = response.json()
    except ValueError as exc:
        msg = "osiptel response is not valid json"
        raise ParseError(msg) from exc

    logger.info(
        "%s %s",
        FETCH_PAGE_OK,
        kv(
            ruc=ruc,
            draw=draw,
            start=start,
            length=length,
            status=status,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        ),
    )
    return payload


def _api_headers() -> dict[str, str]:
    # The proxy-bound client carries the User-Agent and the cookie jar seeded by
    # _ready, so only the request-shaping headers are set here.
    return {
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://checatuslineas.osiptel.gob.pe",
        "Referer": HOME_URL,
    }


def _log_failed(
    status: int, *, ruc: str, draw: int, start: int, length: int, body: str
) -> str:
    text = body.replace("\n", " ").strip()[:160]
    logger.warning(
        "%s %s",
        SITE_REQUEST_FAILED,
        kv(
            site="osiptel",
            status=status,
            ruc=ruc,
            draw=draw,
            start=start,
            length=length,
            body=text,
        ),
    )
    return text


def _is_waf_block(body: str) -> bool:
    normalized = body.lower()
    return (
        "the url you requested has been blocked" in normalized
        or "web page blocked" in normalized
        or "attack id:" in normalized
    )


OSIPTEL = Site(
    name="osiptel",
    columns=("carrier", "lines", "total_lines"),
    # Phone-line counts exist for any taxpayer, natural or juridica.
    supports=frozenset({RucKind.NATURAL, RucKind.JURIDICA}),
    # A RUC with no phone lines is a real, valid empty result, not a fault.
    allows_empty=True,
    # OSIPTEL must rotate every lookup.
    tuning=SiteTuning(session_budget=1),
    endpoints=(_HOME, _API),
    ready=_ready,
    lookup=_lookup,
)
