from __future__ import annotations

import logging

from dataclasses import dataclass
from pathlib import Path

from robot.domain.types import Status, WorkerSummary
from robot.jobs.store import JobStore
from robot.obs.events import WORKER_UNHANDLED_EXCEPTION
from robot.obs.logging import kv
from robot.pipeline.lookup_executor import execute_lookup
from robot.pipeline.session_runtime import SessionRuntime


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerRuntimeConfig:
    run_id: str
    worker_id: int
    store_path: str
    page_size: int
    session_budget: int
    wait_min_s: float
    wait_max_s: float
    ban_cooldown_s: float


class DurableWorker:
    def __init__(self, *, cfg: WorkerRuntimeConfig, geonode) -> None:
        self._cfg = cfg
        self._store = JobStore(path=Path(cfg.store_path))
        self._runtime = SessionRuntime(
            run_id=cfg.run_id,
            worker_id=cfg.worker_id,
            slot_id=cfg.worker_id,
            geonode=geonode,
            session_budget=cfg.session_budget,
            wait_min_s=cfg.wait_min_s,
            wait_max_s=cfg.wait_max_s,
        )

    def run(self) -> WorkerSummary:
        summary = WorkerSummary()
        try:
            while True:
                job = self._store.claim_next(worker_id=self._cfg.worker_id)
                if job is None:
                    break

                result = execute_lookup(
                    run_id=self._cfg.run_id,
                    worker_id=self._cfg.worker_id,
                    runtime=self._runtime,
                    ruc=job.ruc,
                    page_size=self._cfg.page_size,
                    ban_cooldown_s=self._cfg.ban_cooldown_s,
                )
                if result.status == Status.OK:
                    self._store.complete_success(job=job, result=result)
                    summary.succeeded += 1
                else:
                    self._store.complete_failure(job=job, result=result)
                    summary.failed += 1
                summary.processed += 1
        except Exception as exc:
            logger.exception(
                "%s %s",
                WORKER_UNHANDLED_EXCEPTION,
                kv(
                    run_id=self._cfg.run_id,
                    worker_id=self._cfg.worker_id,
                    error_type=type(exc).__name__,
                ),
            )
            raise
        finally:
            self._runtime.close_active(cooldown_s=0.0)
            self._store.close()

        return summary
