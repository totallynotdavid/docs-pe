from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from core.domain.errors import ProviderSchemaError


if TYPE_CHECKING:
    from core.domain.types import Row


@dataclass(frozen=True)
class ParsedPage:
    total_records: int
    rows: tuple[Row, ...]


def parse_page(payload: object) -> ParsedPage:
    if not isinstance(payload, dict):
        msg = "osiptel response json is not an object"
        raise ProviderSchemaError(msg)

    if payload.get("estado") is True:
        msg = "osiptel rejected request"
        raise ProviderSchemaError(msg)

    total_records = _total_records(payload)
    rows = tuple(_line_from_row(row) for row in _rows(payload))

    return ParsedPage(total_records=total_records, rows=rows)


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


def _line_from_row(row: object) -> Row:
    if not isinstance(row, dict):
        msg = "osiptel row has unsupported shape"
        raise ProviderSchemaError(msg)

    modalidad = _required_text(row.get("modalidad"), field="modalidad")
    numero = _required_text(row.get("numeroServicio"), field="numeroServicio")
    operador = _required_text(row.get("operador"), field="operador")

    return (modalidad, numero, operador)


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        msg = f"osiptel row field {field} is empty"
        raise ProviderSchemaError(msg)

    return value.strip()
