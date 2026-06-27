from __future__ import annotations

import logging

from typing import TYPE_CHECKING

from robot.domain.errors import RobotError
from robot.domain.types import RUC, LookupResult, Status
from robot.obs.events import LOOKUP_OK
from robot.obs.logging import kv
from robot.pipeline.attempt import execute_attempt
from robot.pipeline.retry_control import handle_lookup_error


logger = logging.getLogger(__name__)
MAX_ATTEMPTS_PER_RUC = 3


if TYPE_CHECKING:
    from robot.pipeline.session_runtime import SessionRuntime


def execute_lookup(
    *,
    run_id: str,
    worker_id: int,
    runtime: SessionRuntime,
    ruc: RUC,
    page_size: int,
    ban_cooldown_s: float,
) -> LookupResult:
    for attempt_no in range(1, MAX_ATTEMPTS_PER_RUC + 1):
        try:
            success = execute_attempt(runtime=runtime, ruc=ruc, page_size=page_size)
            logger.info(
                "%s %s",
                LOOKUP_OK,
                kv(
                    run_id=run_id,
                    worker_id=worker_id,
                    session_id=success.session_id,
                    proxy_id=success.proxy_id,
                    egress_ip=success.egress_ip,
                    ruc=ruc,
                    attempt=attempt_no,
                    elapsed_ms=success.elapsed_ms,
                    lines=success.total_lines,
                    carriers=len(success.carrier_counts),
                ),
            )
            runtime.after_success()
            return LookupResult(
                ruc=ruc,
                status=Status.OK,
                total_lines=success.total_lines,
                carrier_counts=success.carrier_counts,
                session_id=success.session_id,
                proxy_id=success.proxy_id,
            )
        except RobotError as exc:
            action = handle_lookup_error(
                run_id=run_id,
                worker_id=worker_id,
                runtime=runtime,
                ruc=ruc,
                attempt_no=attempt_no,
                max_attempts=MAX_ATTEMPTS_PER_RUC,
                ban_cooldown_s=ban_cooldown_s,
                error=exc,
            )
            if action.should_retry:
                continue
            if action.failed_result is not None:
                return action.failed_result
            break

    return LookupResult(
        ruc=ruc,
        status=Status.FAILED,
        error_code="exhausted_retries",
        error_detail="unexpected retry exhaustion",
        session_id=runtime.active_session_id(),
        proxy_id=runtime.last_proxy_id,
    )
