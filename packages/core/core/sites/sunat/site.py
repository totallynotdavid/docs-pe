from __future__ import annotations

from typing import TYPE_CHECKING

from core.domain.errors import RucNotFoundError
from core.domain.transport import raise_for_status, warm_endpoints
from core.domain.types import Doc, DocKind, Endpoint, RucKind, Site, SiteTuning
from core.sites.sunat.identity import IDENTITY, fetch_identity
from core.sites.sunat.parser import parse_tipo_documento
from core.sites.sunat.reps import build_reps_body, parse_reps
from core.sites.sunat.request import build_consulta_body, random_token


if TYPE_CHECKING:
    import httpx

    from core.domain.types import Row

HOME_URL = (
    "https://e-consultaruc.sunat.gob.pe/cl-ti-itmrconsruc/FrameCriterioBusquedaWeb.jsp"
)
API_URL = "https://e-consultaruc.sunat.gob.pe/cl-ti-itmrconsruc/jcrS00Alias"
_ORIGIN = "https://e-consultaruc.sunat.gob.pe"

_HOME = Endpoint(name="home", url=HOME_URL)
_CONSULTA = Endpoint(name="consulta", url=API_URL)
_REPS = Endpoint(name="reps", url=API_URL)


async def _ready(client: httpx.AsyncClient, site: Site) -> None:
    await warm_endpoints(client, site.endpoints)


async def _lookup_tipo_documento(
    client: httpx.AsyncClient,
    doc: Doc,
) -> tuple[Row, ...]:
    body = build_consulta_body(ruc=str(doc), token=random_token())
    page = await _post(client, _CONSULTA, body)
    identity = parse_tipo_documento(page)

    if identity is None:
        return ()

    return (
        (
            identity.tipo_doc,
            identity.num_doc,
            identity.nombre,
            identity.tipo_contribuyente,
        ),
    )


async def _lookup_representantes(
    client: httpx.AsyncClient,
    doc: Doc,
) -> tuple[Row, ...]:
    identity = await fetch_identity(client, str(doc))

    if identity is None:
        msg = f"sunat has no record of ruc {doc}"
        raise RucNotFoundError(msg)

    reps = parse_reps(
        await _post(
            client,
            _REPS,
            build_reps_body(ruc=str(doc)),
        )
    )

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
    client: httpx.AsyncClient,
    endpoint: Endpoint,
    body: dict[str, str],
) -> str:
    response = await client.post(
        endpoint.url,
        data=body,
        headers=_headers(),
    )

    raise_for_status(response.status_code, endpoint=endpoint)

    return response.text


def _headers() -> dict[str, str]:
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": _ORIGIN,
        "Referer": HOME_URL,
    }


def _accepts_natural_ruc(doc: Doc) -> bool:
    return doc.kind is DocKind.RUC and doc.ruc_kind is RucKind.NATURAL


def _accepts_juridica_ruc(doc: Doc) -> bool:
    return doc.kind is DocKind.RUC and doc.ruc_kind is RucKind.JURIDICA


# SUNAT sessions are not IP-bound, so one session serves multiple lookups.
_TUNING = SiteTuning(session_budget=50)

SUNAT = Site(
    name="sunat",
    columns=("tipo_doc", "num_doc", "nombre", "tipo_contribuyente"),
    accepts=_accepts_natural_ruc,
    # Every accepted RUC-10 returns at least a name.
    allows_empty=False,
    tuning=_TUNING,
    endpoints=(_HOME,),
    ready=_ready,
    lookup=_lookup_tipo_documento,
    stable=True,
)

SUNAT_REPS = Site(
    name="sunat_reps",
    columns=(
        "razon_social",
        "doc_type",
        "num_doc",
        "nombre",
        "cargo",
        "fecha_desde",
    ),
    accepts=_accepts_juridica_ruc,
    # Some entities have no legal representatives in SUNAT.
    allows_empty=True,
    tuning=_TUNING,
    endpoints=(_HOME, IDENTITY),
    ready=_ready,
    lookup=_lookup_representantes,
    stable=True,
)
