from __future__ import annotations

from typing import TYPE_CHECKING

from fetch.domain.errors import RucNotFoundError
from fetch.domain.transport import raise_for_status, warm_endpoints
from fetch.domain.types import Endpoint, RucKind, Site, SiteTuning
from fetch.sites.sunat.identity import IDENTITY, fetch_identity
from fetch.sites.sunat.parser import parse_tipo_documento
from fetch.sites.sunat.reps import build_reps_body, parse_reps
from fetch.sites.sunat.request import build_consulta_body, random_token


if TYPE_CHECKING:
    import httpx

    from fetch.domain.types import RUC, Row


HOME_URL = (
    "https://e-consultaruc.sunat.gob.pe/cl-ti-itmrconsruc/FrameCriterioBusquedaWeb.jsp"
)
API_URL = "https://e-consultaruc.sunat.gob.pe/cl-ti-itmrconsruc/jcrS00Alias"
_ORIGIN = "https://e-consultaruc.sunat.gob.pe"

# _HOME is warmed by being listed in each site's endpoints tuple. _CONSULTA and
# _REPS are dispatch targets the lookup functions post to; their name feeds the
# status-error message and is never warmed.
_HOME = Endpoint(name="home", url=HOME_URL)
_CONSULTA = Endpoint(name="consulta", url=API_URL)
_REPS = Endpoint(name="reps", url=API_URL)


async def _ready(client: httpx.AsyncClient, site: Site) -> None:
    # SUNAT needs no cookie or captcha handshake; a status-classified GET per
    # declared host is enough.
    await warm_endpoints(client, site.endpoints)


async def _lookup_tipo_documento(
    client: httpx.AsyncClient, ruc: RUC
) -> tuple[Row, ...]:
    body = build_consulta_body(ruc=str(ruc), token=random_token())
    page = await _post(client, _CONSULTA, body)
    record = parse_tipo_documento(page)
    if record is None:
        return ()
    return ((record.tipo_doc, record.num_doc, record.nombre),)


async def _lookup_representantes(
    client: httpx.AsyncClient, ruc: RUC
) -> tuple[Row, ...]:
    # Identity is the existence check: it gates whether the reps request runs at all.
    identity = await fetch_identity(client, str(ruc))
    if identity is None:
        msg = f"sunat has no record of ruc {ruc}"
        raise RucNotFoundError(msg)
    reps = parse_reps(await _post(client, _REPS, build_reps_body(ruc=str(ruc))))
    return tuple(
        (
            identity.razon_social,
            rep.doc_type,
            rep.num_doc,
            rep.nombre,
            rep.cargo,
            rep.fecha_desde,
        )
        for rep in reps
    )


async def _post(
    client: httpx.AsyncClient, endpoint: Endpoint, body: dict[str, str]
) -> str:
    response = await client.post(endpoint.url, data=body, headers=_headers())
    raise_for_status(response.status_code, endpoint=endpoint)
    return response.text


def _headers() -> dict[str, str]:
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": _ORIGIN,
        "Referer": HOME_URL,
    }


# Not IP-bound, so one sticky session serves many lookups. Rotation on budget
# spreads IP-level rate limits, not bans.
_TUNING = SiteTuning(session_budget=50)

SUNAT = Site(
    name="sunat",
    columns=("tipo_doc", "num_doc", "nombre"),
    supports=frozenset({RucKind.NATURAL}),
    # A persona natural always has an identity document, so an empty result is
    # drift, never a valid blank.
    allows_empty=False,
    tuning=_TUNING,
    endpoints=(_HOME,),
    ready=_ready,
    lookup=_lookup_tipo_documento,
)

SUNAT_REPS = Site(
    name="sunat_reps",
    columns=("razon_social", "doc_type", "num_doc", "nombre", "cargo", "fecha_desde"),
    supports=frozenset({RucKind.JURIDICA}),
    # Some entities (associations, educational centers) carry no legal
    # representative in SUNAT's records.
    allows_empty=True,
    tuning=_TUNING,
    endpoints=(_HOME, IDENTITY),
    ready=_ready,
    lookup=_lookup_representantes,
)
