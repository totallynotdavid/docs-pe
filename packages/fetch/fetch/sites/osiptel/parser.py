from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fetch.domain.errors import ProviderSchemaError


@dataclass(frozen=True)
class ParsedPage:
    total_records: int
    rows_returned: int
    carrier_counts: dict[str, int]  # carrier -> lines on this page


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
        total_records=total, rows_returned=len(rows), carrier_counts=counts
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
    if not isinstance(rows, list):
        msg = "osiptel response missing data rows"
        raise ProviderSchemaError(msg)
    return rows


def _carrier_from_row(row: object) -> str:
    if not isinstance(row, dict):
        msg = "osiptel row has unsupported shape"
        raise ProviderSchemaError(msg)
    return _required_text(row.get("operador"), field="operador")


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"osiptel row field {field} is empty"
        raise ProviderSchemaError(msg)
    return value.strip()
