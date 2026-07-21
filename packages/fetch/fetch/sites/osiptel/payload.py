from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PageRequest:
    numero: str
    # OSIPTEL's document-type code: "1" for a DNI, "2" for a RUC.
    id_tipo_doc: str
    draw: int
    start: int
    length: int


def build_payload(req: PageRequest) -> dict[str, str]:
    payload: dict[str, str] = {}
    columns = ["indice", "modalidad", "numeroServicio", "operador"]
    for index, name in enumerate(columns):
        payload[f"columns[{index}][data]"] = name
        payload[f"columns[{index}][name]"] = name
        payload[f"columns[{index}][searchable]"] = "false"
        payload[f"columns[{index}][orderable]"] = "false"
        payload[f"columns[{index}][search][value]"] = ""
        payload[f"columns[{index}][search][regex]"] = "false"

    payload.update(
        {
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
    )
    return payload
