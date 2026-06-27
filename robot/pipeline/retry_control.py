from __future__ import annotations

import logging

from dataclasses import dataclass
from typing import TYPE_CHECKING

from robot.domain.retry import decide_retry
from robot.domain.types import LookupResult, Status
from robot.obs.events import LOOKUP_FAILED
from robot.obs.logging import kv


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryAction:
    should_retry: bool
    failed_result: LookupResult | None = None


def handle_lookup_error(
    *,
    run_id: str,
    worker_id: int,
    runtime: SessionRuntime,
    ruc: RUC,
    attempt_no: int,
    max_attempts: int,
    ban_cooldown_s: float,
    error: RobotError,
) -> RetryAction:
    session_id = runtime.active_session_id()
    proxy_id = runtime.last_proxy_id
    egress_ip = runtime.active_egress_ip()
    decision = decide_retry(
        error,
        default_cooldown_s=ban_cooldown_s,
    )
    logger.warning(
        "%s %s",
        LOOKUP_FAILED,
        kv(
            run_id=run_id,
            worker_id=worker_id,
            session_id=session_id,
            proxy_id=proxy_id,
            egress_ip=egress_ip,
            ruc=ruc,
            attempt=attempt_no,
            error_code=decision.error_code,
            error_detail=str(error),
        ),
    )
    if decision.rotate_session:
        runtime.close_active(cooldown_s=decision.cooldown_proxy_s)

    can_retry = attempt_no < max_attempts and decision.rotate_session
    if can_retry:
        return RetryAction(should_retry=True)
    return RetryAction(
        should_retry=False,
        failed_result=LookupResult(
            ruc=ruc,
            status=Status.FAILED,
            error_code=decision.error_code,
            error_detail=str(error),
            session_id=session_id,
            proxy_id=proxy_id,
        ),
    )


if TYPE_CHECKING:
    from robot.domain.errors import RobotError
    from robot.domain.types import RUC
    from robot.pipeline.session_runtime import SessionRuntime
