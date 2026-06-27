from __future__ import annotations

from robot.domain.types import RUC, CarrierCount
from robot.providers.osiptel.parser import parse_page
from robot.providers.osiptel.payload import PageRequest, build_payload


class OsiptelProvider:
    def __init__(self, *, page_size: int) -> None:
        self._page_size = page_size

    def lookup_ruc(self, *, session, ruc: RUC) -> tuple[int, tuple[CarrierCount, ...]]:
        total: int | None = None
        start = 0
        draw = 1
        counts: dict[str, int] = {}

        while True:
            payload = session.fetch_json(
                build_payload(
                    PageRequest(
                        ruc=str(ruc),
                        draw=draw,
                        start=start,
                        length=self._page_size,
                    )
                ),
                ruc=str(ruc),
                draw=draw,
                start=start,
                length=self._page_size,
            )
            page = parse_page(payload)
            if total is None or total != page.total_records:
                total = page.total_records

            for carrier in page.carrier_counts:
                counts[carrier.carrier] = counts.get(carrier.carrier, 0) + carrier.lines

            if total == 0 or page.rows_returned == 0:
                break
            start += page.rows_returned
            draw += 1
            if start >= total:
                break

        return total or 0, tuple(
            CarrierCount(carrier=name, lines=lines)
            for name, lines in sorted(counts.items())
        )
