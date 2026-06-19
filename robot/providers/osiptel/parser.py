from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from robot.domain.errors import ProviderSchemaError
from robot.domain.types import CarrierCount


@dataclass(frozen=True)
class ParsedPage:
    total_records: int
    rows_returned: int
    carrier_counts: tuple[CarrierCount, ...]


def parse_page(payload: object) -> ParsedPage:
    if not isinstance(payload, dict):
        msg = "osiptel response json is not an object"
        raise ProviderSchemaError(msg)
    if payload.get("estado") is True:
        msg = "osiptel rejected request"
        raise ProviderSchemaError(msg)

    total = _total_records(payload)
    rows = _rows(payload)
    counts: dict[str, int] = {}
    for row in rows:
        carrier = _carrier_from_row(row)
        counts[carrier] = counts.get(carrier, 0) + 1

    return ParsedPage(
        total_records=total,
        rows_returned=len(rows),
        carrier_counts=tuple(
            CarrierCount(carrier=name, lines=lines)
            for name, lines in sorted(counts.items())
        ),
    )


def _total_records(payload: dict[Any, Any]) -> int:
    value = payload.get("iTotalRecords")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    msg = "osiptel response missing iTotalRecords"
    raise ProviderSchemaError(msg)


def _rows(payload: dict[Any, Any]) -> list[Any]:
    rows = payload.get("data")
    if rows is None:
        rows = payload.get("aaData")
    if not isinstance(rows, list):
        msg = "osiptel response missing data rows"
        raise ProviderSchemaError(msg)
    return rows


def _carrier_from_row(row: object) -> str:
    if isinstance(row, dict):
        return _required_text(row.get("operador"), field="operador")
    if isinstance(row, list):
        if len(row) <= 3:
            msg = "osiptel legacy row is missing operador column"
            raise ProviderSchemaError(msg)
        return _required_text(row[3], field="operador")
    msg = "osiptel row has unsupported shape"
    raise ProviderSchemaError(msg)


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"osiptel row field {field} is empty"
        raise ProviderSchemaError(msg)
    return value.strip()

