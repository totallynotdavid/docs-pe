from __future__ import annotations

import asyncio
import logging
import time

from typing import TYPE_CHECKING, Any

import httpx

from core.domain.errors import (
    BanSignalError,
    ParseError,
    TransientTransportError,
    UpstreamNotReadyError,
)
from core.domain.transport import classify_status
from core.domain.types import (
    Doc,
    Endpoint,
    Projection,
    Site,
    SiteTuning,
)
from core.obs.events import FETCH_PAGE_OK, FETCH_PAGE_START, SITE_REQUEST_FAILED
from core.obs.logging import kv
from core.sites.osiptel.parser import parse_page
from core.sites.osiptel.payload import PageRequest, build_payload


if TYPE_CHECKING:
    from core.domain.types import Row

logger = logging.getLogger(__name__)

HOME_URL = "https://checatuslineas.osiptel.gob.pe/"
API_URL = "https://checatuslineas.osiptel.gob.pe/Consultas/GetAllCabeceraConsulta/"

PAGE_SIZE = 5_000
_READY_TIMEOUT_S = 25.0
_READY_POLL_S = 0.25

_HOME = Endpoint(name="home", url=HOME_URL)
_API = Endpoint(name="api", url=API_URL)


async def _ready(client: httpx.AsyncClient, site: Site) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_S
    last_status = 0
    last_body = ""
    last_error = ""

    while time.monotonic() < deadline:
        # Redirect errors bypass the custom transport normalization.
        try:
            response = await client.get(_HOME.url)
        except (TransientTransportError, httpx.HTTPError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(_READY_POLL_S)
            continue

        last_status = response.status_code
        last_body = response.text.replace("\n", " ").strip()[:240]

        # The WAF may return HTTP 200 with a CAPTCHA page.
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
    start = 0
    draw = 1
    rows: list[Row] = []

    while True:
        payload = await _fetch_page(
            client,
            doc=doc,
            draw=draw,
            start=start,
            length=PAGE_SIZE,
        )
        page = parse_page(payload)
        rows.extend(page.rows)

        if page.total_records == 0 or not page.rows:
            break

        start += len(page.rows)
        draw += 1

        if start >= page.total_records:
            break

    return tuple(rows)


def _counts(rows: tuple[Row, ...]) -> tuple[Row, ...]:
    counts: dict[str, int] = {}

    for _modalidad, _numero, operador in rows:
        carrier = str(operador)
        counts[carrier] = counts.get(carrier, 0) + 1

    total = len(rows)

    return tuple((carrier, lines, total) for carrier, lines in sorted(counts.items()))


async def _fetch_page(
    client: httpx.AsyncClient,
    *,
    doc: Doc,
    draw: int,
    start: int,
    length: int,
) -> Any:
    started = time.perf_counter()
    numero = str(doc)

    logger.info(
        "%s %s",
        FETCH_PAGE_START,
        kv(doc=numero, draw=draw, start=start, length=length),
    )

    data = build_payload(
        PageRequest.for_doc(
            doc,
            draw=draw,
            start=start,
            length=length,
        )
    )

    response = await client.post(
        _API.url,
        data=data,
        headers=_api_headers(),
    )

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
        msg_0 = "osiptel response is not valid json"
        raise ParseError(msg_0) from exc

    logger.info(
        "%s %s",
        FETCH_PAGE_OK,
        kv(
            doc=numero,
            draw=draw,
            start=start,
            length=length,
            status=status,
            elapsed_ms=int((time.perf_counter() - started) * 1_000),
        ),
    )

    return payload


def _api_headers() -> dict[str, str]:
    # The client already carries the User-Agent and cookies seeded by `_ready`.
    return {
        "Accept": "*/*",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": "https://checatuslineas.osiptel.gob.pe",
        "Referer": HOME_URL,
    }


def _log_failed(
    status: int,
    *,
    numero: str,
    draw: int,
    start: int,
    length: int,
    body: str,
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


def _accepts_any(_doc: Doc) -> bool:
    return True


_COUNTS = Projection(
    name="counts",
    columns=("carrier", "lines", "total_lines"),
    project=_counts,
)

OSIPTEL = Site(
    name="osiptel",
    columns=("modalidad", "numero", "operador"),
    accepts=_accepts_any,
    # No phone lines is a valid result.
    allows_empty=True,
    # Every lookup requires a fresh warmup.
    tuning=SiteTuning(session_budget=1),
    endpoints=(_HOME,),
    ready=_ready,
    lookup=_lookup,
    projections=(_COUNTS,),
    stable=True,
)
