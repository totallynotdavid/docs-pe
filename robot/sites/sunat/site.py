from __future__ import annotations

from typing import TYPE_CHECKING

from robot.domain.errors import BanSignalError, TransientTransportError
from robot.domain.types import RucKind, Site, SiteTuning
from robot.sites.sunat.parser import (
    parse_razon_social,
    parse_reps,
    parse_tipo_documento,
)
from robot.sites.sunat.request import (
    build_consulta_body,
    build_reps_body,
    random_token,
)


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


async def _lookup_tipo_documento(
    client: httpx.AsyncClient, ruc: RUC
) -> tuple[Row, ...]:
    body = build_consulta_body(ruc=str(ruc), token=random_token())
    page = await _post(client, body, what="consulta")
    record = parse_tipo_documento(page)
    if record is None:
        return ()
    return ((record.tipo_doc, record.num_doc, record.nombre),)


async def _lookup_representantes(
    client: httpx.AsyncClient, ruc: RUC
) -> tuple[Row, ...]:
    # Two requests: the ficha RUC carries the razon social (getRepLeg only echoes
    # back whatever name we send), then getRepLeg carries the representatives table.
    consulta_body = build_consulta_body(ruc=str(ruc), token=random_token())
    razon_social = parse_razon_social(
        await _post(client, consulta_body, what="consulta")
    )
    reps_body = build_reps_body(ruc=str(ruc), razon_social=razon_social)
    reps = parse_reps(await _post(client, reps_body, what="reps"))
    return tuple(
        (
            razon_social,
            rep.doc_type,
            rep.num_doc,
            rep.nombre,
            rep.cargo,
            rep.fecha_desde,
        )
        for rep in reps
    )


async def _post(client: httpx.AsyncClient, body: dict[str, str], *, what: str) -> str:
    response = await client.post(API_URL, data=body, headers=_headers())
    status = response.status_code
    if status in _TRANSIENT_STATUSES:
        msg = f"sunat {what} transient status={status}"
        raise TransientTransportError(msg)
    if status >= 500 or status in _BAN_STATUSES:
        msg = f"sunat {what} failed status={status}"
        raise BanSignalError(msg)
    if status != 200:
        msg = f"sunat {what} failed status={status}"
        raise TransientTransportError(msg)
    return response.text


def _headers() -> dict[str, str]:
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": _ORIGIN,
        "Referer": HOME_URL,
    }


# Not IP-bound (see _ready), so one sticky session serves many lookups; the proxy
# still rotates on budget to spread IP-level rate limits.
_TUNING = SiteTuning(session_budget=50)

SUNAT = Site(
    name="sunat",
    columns=("tipo_doc", "num_doc", "nombre"),
    supports=frozenset({RucKind.NATURAL}),
    # A persona natural is defined by an identity document, so it is always present;
    # an empty result is drift, never a valid blank.
    allows_empty=False,
    tuning=_TUNING,
    ready=_ready,
    lookup=_lookup_tipo_documento,
)

SUNAT_REPS = Site(
    name="sunat_reps",
    columns=("razon_social", "doc_type", "num_doc", "nombre", "cargo", "fecha_desde"),
    supports=frozenset({RucKind.JURIDICA}),
    # Some entities (associations, educational centers) don't carry any legal
    # representative in SUNAT's records.
    allows_empty=True,
    tuning=_TUNING,
    ready=_ready,
    lookup=_lookup_representantes,
)
