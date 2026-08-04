from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from fetch.domain.types import DocKind


if TYPE_CHECKING:
    from fetch.domain.types import Doc

_COLUMNS = ("indice", "modalidad", "numeroServicio", "operador")


@dataclass(frozen=True)
class PageRequest:
    numero: str
    id_tipo_doc: Literal["1", "2"]
    draw: int
    start: int
    length: int

    @classmethod
    def for_doc(
        cls,
        doc: Doc,
        *,
        draw: int,
        start: int,
        length: int,
    ) -> PageRequest:
        return cls(
            numero=str(doc),
            id_tipo_doc="1" if doc.kind is DocKind.DNI else "2",
            draw=draw,
            start=start,
            length=length,
        )


def build_payload(req: PageRequest) -> dict[str, str]:
    payload = {
        "order[0][column]": "0",
        "order[0][dir]": "asc",
        "draw": str(req.draw),
        "start": str(req.start),
        "length": str(req.length),
        "search[value]": "",
        "search[regex]": "false",
        "IdTipoDoc": req.id_tipo_doc,
        "NumeroDocumento": req.numero,
        "HCaptchaTokenCon": "",
        "BoolConsulta": "true",
    }

    for index, name in enumerate(_COLUMNS):
        payload[f"columns[{index}][data]"] = name
        payload[f"columns[{index}][name]"] = name
        payload[f"columns[{index}][searchable]"] = "false"
        payload[f"columns[{index}][orderable]"] = "false"
        payload[f"columns[{index}][search][value]"] = ""
        payload[f"columns[{index}][search][regex]"] = "false"

    return payload
