from __future__ import annotations

import json

from typing import TYPE_CHECKING

from core.domain.errors import ProviderSchemaError


if TYPE_CHECKING:
    from core.domain.types import Row


def encode_rows(rows: tuple[Row, ...]) -> str:
    return json.dumps([list(row) for row in rows], ensure_ascii=False)


def decode_rows(text: str) -> tuple[Row, ...]:
    # The one dynamic hop in the store; validate and narrow it here so a hand-edited
    # or corrupt payload fails loudly.
    raw = json.loads(text)
    if not isinstance(raw, list):
        msg = "stored payload is not a list"
        raise ProviderSchemaError(msg)
    rows: list[Row] = []
    for item in raw:
        if not isinstance(item, list) or not all(
            isinstance(cell, (str, int)) for cell in item
        ):
            msg = "stored payload row is malformed"
            raise ProviderSchemaError(msg)
        rows.append(tuple(item))
    return tuple(rows)
