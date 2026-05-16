from __future__ import annotations

import logging

from typing import TYPE_CHECKING, Protocol

from robot.domain.types import RUC, LookupResult, Status, WorkerSummary
from robot.obs.events import WORKER_UNHANDLED_EXCEPTION
from robot.obs.logging import kv
from robot.pipeline.lookup_executor import execute_lookup
from robot.pipeline.session_runtime import SessionRuntime


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from robot.pipeline.settings import WorkerSettings
    from robot.providers.geonode import GeoNodeConfig


class ResultWriter(Protocol):
    def write(self, result: LookupResult) -> None: ...


class TaskQueue(Protocol):
    def get(self) -> RUC | None: ...
    def task_done(self) -> None: ...


class Worker:
    def __init__(
        self,
        *,
        worker_id: int,
        slot_id: int,
        run_id: str,
        task_queue: TaskQueue,
        writer: ResultWriter,
        settings: WorkerSettings,
        geonode: GeoNodeConfig,
    ) -> None:
        self._worker_id = worker_id
        self._run_id = run_id
        self._task_queue = task_queue
        self._writer = writer
        self._settings = settings
        self._runtime = SessionRuntime(
            run_id=run_id,
            worker_id=worker_id,
            slot_id=slot_id,
            geonode=geonode,
            chrome_binary=settings.chrome_binary,
            session_budget=settings.session_budget,
            wait_min_s=settings.wait_min_s,
            wait_max_s=settings.wait_max_s,
            captcha_timeout_s=settings.captcha_timeout_s,
            captcha_timeout_first_s=settings.captcha_timeout_first_s,
            captcha_same_session_retries=settings.captcha_same_session_retries,
            captcha_first_token_jitter_max_s=settings.captcha_first_token_jitter_max_s,
        )

    def run(self) -> WorkerSummary:
        summary = WorkerSummary()
        try:
            while True:
                ruc = self._task_queue.get()
                if ruc is None:
                    self._task_queue.task_done()
                    break

                try:
                    result = self._process_ruc(ruc)
                except Exception as exc:
                    logger.exception(
                        "%s %s",
                        WORKER_UNHANDLED_EXCEPTION,
                        kv(
                            run_id=self._run_id,
                            worker_id=self._worker_id,
                            ruc=ruc,
                            error_type=type(exc).__name__,
                        ),
                    )
                    session_id = self._runtime.active_session_id()
                    proxy_id = self._runtime.last_proxy_id
                    self._runtime.close_active(cooldown_s=0.0)
                    result = LookupResult(
                        ruc=ruc,
                        status=Status.FAILED,
                        error_code="unexpected_worker_error",
                        error_detail=f"{type(exc).__name__}: {exc}",
                        attempt=0,
                        session_id=session_id,
                        proxy_id=proxy_id,
                    )
                finally:
                    self._task_queue.task_done()

                self._writer.write(result)
                summary.processed += 1
                if result.status == Status.OK:
                    summary.succeeded += 1
                else:
                    summary.failed += 1
        finally:
            self._runtime.close_active(cooldown_s=0.0)
        return summary

    def _process_ruc(self, ruc: RUC) -> LookupResult:
        return execute_lookup(
            run_id=self._run_id,
            worker_id=self._worker_id,
            runtime=self._runtime,
            ruc=ruc,
            page_size=self._settings.page_size,
            ban_cooldown_s=self._settings.ban_cooldown_s,
        )
