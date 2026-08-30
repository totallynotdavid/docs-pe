from __future__ import annotations

import json

from dataclasses import dataclass
from typing import TYPE_CHECKING

from core.domain.errors import ProviderSchemaError
from core.domain.transport import raise_for_status
from core.domain.types import Endpoint


if TYPE_CHECKING:
    import httpx


# Representative lookups warm this endpoint before requesting their data.
IDENTITY = Endpoint(
    name="identity",
    url="https://ww1.sunat.gob.pe/ol-ti-itfisdenreg/itfisdenreg.htm",
)


@dataclass(frozen=True)
class IdentityRecord:
    razon_social: str


async def fetch_identity(
    client: httpx.AsyncClient,
    ruc: str,
) -> IdentityRecord | None:
    response = await client.get(
        IDENTITY.url,
        params={"accion": "obtenerDatosRuc", "nroRuc": ruc},
    )
    raise_for_status(response.status_code, endpoint=IDENTITY)
    return parse_identity(response.text)


def parse_identity(payload_text: str) -> IdentityRecord | None:
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        msg = "sunat identity response is not valid json"
        raise ProviderSchemaError(msg) from exc

    if not isinstance(payload, dict):
        msg = "sunat identity response is not a json object"
        raise ProviderSchemaError(msg)

    # The endpoint uses an `error` object for an unregistered RUC.
    if "lista" not in payload:
        if "error" in payload:
            return None

        msg = "sunat identity response has no lista or error"
        raise ProviderSchemaError(msg)

    try:
        razon_social = payload["lista"][0]["apenomdenunciado"].strip()
    except (AttributeError, KeyError, IndexError, TypeError) as exc:
        msg = "sunat identity response is missing the expected fields"
        raise ProviderSchemaError(msg) from exc

    if not razon_social:
        msg = "sunat identity response has an empty razon social"
        raise ProviderSchemaError(msg)

    return IdentityRecord(razon_social=razon_social)
