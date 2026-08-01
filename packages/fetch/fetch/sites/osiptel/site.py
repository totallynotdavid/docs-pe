from __future__ import annotations

import asyncio
import logging
import time

from typing import TYPE_CHECKING, Any

import httpx

from fetch.domain.errors import (
    BanSignalError,
    ParseError,
    TransientTransportError,
    UpstreamNotReadyError,
)
from fetch.domain.transport import classify_status
from fetch.domain.types import Doc, DocKind, Endpoint, Projection, Site, SiteTuning
from fetch.obs.events import FETCH_PAGE_OK, FETCH_PAGE_START, SITE_REQUEST_FAILED
from fetch.obs.logging import kv
from fetch.sites.osiptel.parser import parse_page
from fetch.sites.osiptel.payload import PageRequest, build_payload


if TYPE_CHECKING:
    from fetch.domain.types import Row


logger = logging.getLogger(__name__)

HOME_URL = "https://checatuslineas.osiptel.gob.pe/"
API_URL = "https://checatuslineas.osiptel.gob.pe/Consultas/GetAllCabeceraConsulta/"
# OSIPTEL caps each page at 5000 rows; the loop in _lookup pages past the cap.
PAGE_SIZE = 5000
_READY_TIMEOUT_S = 25.0
_READY_POLL_S = 0.25

_HOME = Endpoint(name="home", url=HOME_URL)
_API = Endpoint(name="api", url=API_URL)


async def _ready(client: httpx.AsyncClient, site: Site) -> None:
    # Poll the home page to seed cookies (kept in the client jar and auto-sent on the
    # API POST) and to detect a WAF block on this exit before spending a lookup.
    deadline = time.monotonic() + _READY_TIMEOUT_S
    last_status = 0
    last_body = ""
    last_error = ""
    while time.monotonic() < deadline:
        try:
            response = await client.get(_HOME.url)
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


async def _lookup(client: httpx.AsyncClient, doc: Doc) -> tuple[Row, ...]:
    numero = str(doc)
    id_tipo_doc = _id_tipo_doc(doc)
    total: int | None = None
    start = 0
    draw = 1
    rows: list[Row] = []

    while True:
        payload = await _fetch_page(
            client,
            numero=numero,
            id_tipo_doc=id_tipo_doc,
            draw=draw,
            start=start,
            length=PAGE_SIZE,
        )
        page = parse_page(payload)
        total = page.total_records
        rows.extend(page.rows)

        if total == 0 or len(page.rows) == 0:
            break
        start += len(page.rows)
        draw += 1
        if start >= total:
            break

    return tuple(rows)


def _id_tipo_doc(doc: Doc) -> str:
    return "1" if doc.kind is DocKind.DNI else "2"


def _counts(rows: tuple[Row, ...]) -> tuple[Row, ...]:
    # Fold the per-line rows into per-carrier counts; total_lines is the doc's grand
    # total. A pure projection over stored rows, so counts never costs a second fetch.
    counts: dict[str, int] = {}
    for _modalidad, _numero, operador in rows:
        key = str(operador)
        counts[key] = counts.get(key, 0) + 1
    total = len(rows)
    return tuple((carrier, lines, total) for carrier, lines in sorted(counts.items()))


async def _fetch_page(
    client: httpx.AsyncClient,
    *,
    numero: str,
    id_tipo_doc: str,
    draw: int,
    start: int,
    length: int,
) -> Any:
    started = time.perf_counter()
    logger.info(
        "%s %s",
        FETCH_PAGE_START,
        kv(doc=numero, draw=draw, start=start, length=length),
    )
    data = build_payload(
        PageRequest(
            numero=numero,
            id_tipo_doc=id_tipo_doc,
            draw=draw,
            start=start,
            length=length,
        )
    )
    # Transport faults are normalized to TransientTransportError by the client
    # transport (proxy/transport.py), so there is nothing to catch here.
    response = await client.post(_API.url, data=data, headers=_api_headers())

    status = response.status_code
    fault = classify_status(status)
    if fault is not None:
        body = _log_failed(
            status,
            numero=numero,
            draw=draw,
            start=start,
            length=length,
            body=response.text,
        )
        if fault is BanSignalError:
            msg = (
                "osiptel request failed "
                f"status={status} draw={draw} start={start} length={length} "
                f"doc={numero} body={body}"
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
            doc=numero,
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
    status: int, *, numero: str, draw: int, start: int, length: int, body: str
) -> str:
    text = body.replace("\n", " ").strip()[:160]
    logger.warning(
        "%s %s",
        SITE_REQUEST_FAILED,
        kv(
            site="osiptel",
            status=status,
            doc=numero,
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


def _accepts_any(doc: Doc) -> bool:
    # The public endpoint answers any document type (DNI or RUC), keyed by IdTipoDoc.
    return True


_COUNTS = Projection(
    name="counts",
    columns=("carrier", "lines", "total_lines"),
    project=_counts,
)


OSIPTEL = Site(
    name="osiptel",
    # The raw truth is one row per line: its modality, redacted number, and carrier.
    columns=("modalidad", "numero", "operador"),
    accepts=_accepts_any,
    # A document with no phone lines is a real, valid empty result, not a fault.
    allows_empty=True,
    # OSIPTEL must rotate every lookup.
    tuning=SiteTuning(session_budget=1),
    endpoints=(_HOME,),
    ready=_ready,
    lookup=_lookup,
    # Per-carrier line counts, folded from the stored rows at export time.
    projections=(_COUNTS,),
    stable=True,
)
