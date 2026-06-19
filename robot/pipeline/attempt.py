from __future__ import annotations

import time

from dataclasses import dataclass
from typing import TYPE_CHECKING

from robot.providers.osiptel_flow import count_carrier_lines


@dataclass(frozen=True)
class AttemptSuccess:
    session_id: str
    proxy_id: str
    egress_ip: str
    total_lines: int
    carrier_counts: tuple[CarrierCount, ...]
    elapsed_ms: int


def execute_attempt(
    *, runtime: SessionRuntime, ruc: RUC, page_size: int
) -> AttemptSuccess:
    active = runtime.ensure_active()
    runtime.refresh_egress_ip()
    started = time.perf_counter()
    total, carrier_counts = count_carrier_lines(
        session=active.osiptel,
        ruc=ruc,
        page_size=page_size,
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return AttemptSuccess(
        session_id=active.osiptel.session_id,
        proxy_id=active.osiptel.proxy_id,
        egress_ip=active.egress_ip,
        total_lines=total,
        carrier_counts=carrier_counts,
        elapsed_ms=elapsed_ms,
    )


if TYPE_CHECKING:
    from robot.domain.types import RUC, CarrierCount
    from robot.pipeline.session_runtime import SessionRuntime
