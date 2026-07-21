from __future__ import annotations

from pathlib import Path

from capture.sites.base import CaptureSite
from capture.sites.entel.parse import parse_lookup_result


def _row(ruc: str, columns: dict[str, str], observed_at: str) -> list[str]:
    return [ruc, columns["debt_total"], columns["has_punishment"], observed_at]


ENTEL = CaptureSite(
    name="entel",
    origin="https://miperfil.entel.pe",
    export_header=("ruc", "debt_total", "has_punishment", "observed_at"),
    script=Path(__file__).with_name("capture.js"),
    parse=parse_lookup_result,
    row=_row,
)
