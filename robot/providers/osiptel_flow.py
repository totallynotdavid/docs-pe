from __future__ import annotations

import logging

from typing import TYPE_CHECKING, Any

from robot.domain.types import RUC, CarrierCount
from robot.providers.osiptel_client import OsiptelClient, OsiptelResponse, PageRequest


if TYPE_CHECKING:
    from robot.providers.osiptel_session import OsiptelSession


logger = logging.getLogger(__name__)


def count_carrier_lines(
    *,
    session: OsiptelSession,
    ruc: RUC,
    page_size: int,
) -> tuple[int, tuple[CarrierCount, ...]]:
    user_agent = session.user_agent()
    cookie_header = session.cookie_header()

    total: int | None = None
    start = 0
    draw = 1
    counts: dict[str, int] = {}

    with OsiptelClient(
        proxy=session.proxy_config,
        user_agent=user_agent,
        cookie_header=cookie_header,
    ) as client:
        while True:
            payload = client.fetch(
                PageRequest(
                    ruc=str(ruc),
                    draw=draw,
                    start=start,
                    length=page_size,
                )
            )

            if total is None:
                total = _total_records(payload)

            rows = payload.get("data") or payload.get("aaData") or []
            if not isinstance(rows, list):
                rows = []
            for carrier in _carrier_counts(rows):
                counts[carrier.carrier] = counts.get(carrier.carrier, 0) + carrier.lines

            if total == 0 or not rows:
                break

            start += len(rows)
            draw += 1
            if start >= total:
                break

    carrier_rows = tuple(
        CarrierCount(carrier=name, lines=lines)
        for name, lines in sorted(counts.items())
    )
    return total or 0, carrier_rows


def _total_records(payload: OsiptelResponse) -> int:
    value = payload.get("iTotalRecords")
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _carrier_counts(rows: list[Any]) -> tuple[CarrierCount, ...]:
    counts: dict[str, int] = {}
    for row in rows:
        carrier = _carrier_from_row(row)
        if not carrier:
            continue
        counts[carrier] = counts.get(carrier, 0) + 1
    return tuple(
        CarrierCount(carrier=name, lines=lines) for name, lines in counts.items()
    )


def _carrier_from_row(row: Any) -> str:
    if isinstance(row, dict):
        return _as_text(row.get("operador"))
    if isinstance(row, list):
        return _as_text(_pick(row, 3))
    return ""


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    return ""


def _pick(row: list[Any], idx: int) -> Any:
    if idx < 0 or idx >= len(row):
        return ""
    return row[idx]
