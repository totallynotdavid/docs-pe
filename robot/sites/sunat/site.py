from __future__ import annotations

from typing import TYPE_CHECKING

from robot.domain.errors import BanSignalError, TransientTransportError
from robot.domain.types import Site, SiteTuning
from robot.sites.sunat.parser import parse_page
from robot.sites.sunat.request import build_body, random_token


if TYPE_CHECKING:
    import httpx

    from robot.domain.types import RUC, Row


HOME_URL = (
    "https://e-consultaruc.sunat.gob.pe/cl-ti-itmrconsruc/FrameCriterioBusquedaWeb.jsp"
)
API_URL = "https://e-consultaruc.sunat.gob.pe/cl-ti-itmrconsruc/jcrS00Alias"
_ORIGIN = "https://e-consultaruc.sunat.gob.pe"
_TRANSIENT_STATUSES = frozenset({502, 503, 504})
_BAN_STATUSES = frozenset({403, 429})


async def _ready(client: httpx.AsyncClient) -> None:
    # SUNAT is effectively stateless (no cookie or captcha handshake is required,
    # verified live), so this GET only warms the client and fails a dead proxy fast
    # before a lookup attempt is spent on it.
    response = await client.get(HOME_URL)
    if response.status_code >= 500 or response.status_code in _BAN_STATUSES:
        msg = f"sunat home not ready status={response.status_code}"
        raise BanSignalError(msg)


async def _lookup(client: httpx.AsyncClient, ruc: RUC) -> tuple[Row, ...]:
    body = build_body(ruc=str(ruc), token=random_token())
    response = await client.post(API_URL, data=body, headers=_headers())

    status = response.status_code
    if status in _TRANSIENT_STATUSES:
        msg = f"sunat request transient status={status}"
        raise TransientTransportError(msg)
    if status >= 500 or status in _BAN_STATUSES:
        msg = f"sunat request failed status={status}"
        raise BanSignalError(msg)
    if status != 200:
        msg = f"sunat request failed status={status}"
        raise TransientTransportError(msg)

    record = parse_page(response.text)
    if record is None:
        return ()
    return ((record.tipo_doc, record.num_doc, record.nombre),)


def _headers() -> dict[str, str]:
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": _ORIGIN,
        "Referer": HOME_URL,
    }


SUNAT = Site(
    name="sunat",
    columns=("tipo_doc", "num_doc", "nombre"),
    # Not IP-bound (see _ready), so one sticky session serves many lookups; the
    # proxy still rotates on budget to spread IP-level rate limits.
    tuning=SiteTuning(session_budget=50),
    ready=_ready,
    lookup=_lookup,
)
