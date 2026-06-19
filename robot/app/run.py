from __future__ import annotations

import logging

from typing import TYPE_CHECKING

from robot.domain.types import RunSummary
from robot.jobs.exporter import export_csv
from robot.jobs.planner import plan_jobs
from robot.jobs.store import JobStore, state_path_for_output
from robot.obs.events import RUN_SUMMARY
from robot.obs.logging import kv
from robot.workers.supervisor import run_workers


if TYPE_CHECKING:
    from robot.app.config import RunConfig


logger = logging.getLogger(__name__)


def run(cfg: RunConfig, *, run_id: str) -> None:
    store_path = state_path_for_output(cfg.output_csv)
    state_existed = store_path.exists()
    with JobStore(store_path) as store:
        seeded = 0
        if not state_existed:
            seeded = store.seed_success_csv(cfg.output_csv)
        reset = store.reset_running()
        plan = plan_jobs(input_csv=cfg.input_csv, store=store, dedupe=cfg.dedupe)
        planned_totals = store.summary()

    if planned_totals.pending > 0:
        summary = run_workers(
            run_id=run_id,
            store_path=str(store_path),
            env_file=cfg.env_file,
            worker_count=cfg.workers,
            page_size=cfg.page_size,
            session_budget=cfg.session_budget,
            wait_min_s=cfg.wait_min_s,
            wait_max_s=cfg.wait_max_s,
            ban_cooldown_s=cfg.ban_cooldown_s,
            debug=cfg.debug,
        )
    else:
        summary = RunSummary()

    with JobStore(store_path) as store:
        export_csv(store=store, output_csv=cfg.output_csv)
        totals = store.summary()

    summary.rows_read = plan.rows_read
    summary.valid = plan.valid
    summary.ignored = plan.ignored
    summary.duplicates = plan.duplicates
    summary.skipped = plan.skipped

    logger.info(
        "job_store_summary %s",
        kv(
            run_id=run_id,
            state_db=store_path,
            seeded=seeded,
            reset_running=reset,
            inserted=plan.inserted,
            pending=totals.pending,
            running=totals.running,
            succeeded=totals.succeeded,
            failed=totals.failed,
        ),
    )

    logger.info(
        "%s %s",
        RUN_SUMMARY,
        kv(
            run_id=run_id,
            rows_read=summary.rows_read,
            valid=summary.valid,
            ignored=summary.ignored,
            duplicates=summary.duplicates,
            skipped=summary.skipped,
            processed=summary.processed,
            succeeded=summary.succeeded,
            failed=summary.failed,
        ),
    )
