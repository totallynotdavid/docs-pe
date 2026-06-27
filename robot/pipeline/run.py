from __future__ import annotations

import asyncio
import logging

from typing import TYPE_CHECKING

from robot.domain.types import LaneTotals, RunReport
from robot.jobs.exporter import export_csv
from robot.jobs.planner import plan_jobs
from robot.jobs.store import JobStore, state_path_for_output
from robot.obs.events import PROVIDER_SELECTED, RUN_SUMMARY
from robot.obs.logging import kv
from robot.pipeline.lane import LaneConfig, ProxyLane
from robot.providers.proxy import load_proxy_provider


if TYPE_CHECKING:
    from robot.cli import RunConfig
    from robot.domain.types import RUC
    from robot.providers.proxy import ProxyProvider


logger = logging.getLogger(__name__)


async def run(cfg: RunConfig, *, run_id: str) -> RunReport:
    store_path = state_path_for_output(cfg.output_csv)
    state_existed = store_path.exists()

    with JobStore(store_path) as store:
        seeded = 0 if state_existed else store.seed_success_csv(cfg.output_csv)
        plan = plan_jobs(input_csv=cfg.input_csv, store=store, dedupe=cfg.dedupe)
        pending = store.pending_rucs()

        totals = LaneTotals()
        try:
            if pending:
                provider = load_proxy_provider(env_file=cfg.env_file)
                workers = (
                    cfg.workers if cfg.workers is not None else provider.tuning.workers
                )
                ban_cooldown_s = (
                    cfg.ban_cooldown_s
                    if cfg.ban_cooldown_s is not None
                    else provider.tuning.ban_cooldown_s
                )
                logger.info(
                    "%s %s",
                    PROVIDER_SELECTED,
                    kv(
                        run_id=run_id,
                        provider=provider.name,
                        workers=workers,
                        ban_cooldown_s=ban_cooldown_s,
                        pending=len(pending),
                    ),
                )
                totals = await _drive_lanes(
                    cfg=cfg,
                    store=store,
                    provider=provider,
                    workers=workers,
                    ban_cooldown_s=ban_cooldown_s,
                    pending=pending,
                    run_id=run_id,
                )
        finally:
            # Export from whatever the store holds even on interrupt: the store
            # is durable, so a partial run still yields usable CSVs.
            export_csv(store=store, output_csv=cfg.output_csv)
            store_summary = store.summary()

        report = RunReport(
            rows_read=plan.rows_read,
            valid=plan.valid,
            ignored=plan.ignored,
            duplicates=plan.duplicates,
            skipped=plan.skipped,
            inserted=plan.inserted,
            seeded=seeded,
            processed=totals.processed,
            succeeded=totals.succeeded,
            failed=totals.failed,
            pending=store_summary.pending,
            failed_jobs=store_summary.failed,
        )

    logger.info(
        "%s %s",
        RUN_SUMMARY,
        kv(
            run_id=run_id,
            state_db=store_path,
            rows_read=report.rows_read,
            valid=report.valid,
            ignored=report.ignored,
            duplicates=report.duplicates,
            skipped=report.skipped,
            inserted=report.inserted,
            seeded=report.seeded,
            processed=report.processed,
            succeeded=report.succeeded,
            failed=report.failed,
            pending=report.pending,
            failed_jobs=report.failed_jobs,
        ),
    )
    return report


async def _drive_lanes(
    *,
    cfg: RunConfig,
    store: JobStore,
    provider: ProxyProvider,
    workers: int,
    ban_cooldown_s: float,
    pending: list[RUC],
    run_id: str,
) -> LaneTotals:
    lane_count = min(workers, len(pending))
    feed: asyncio.Queue[RUC | None] = asyncio.Queue()
    for ruc in pending:
        feed.put_nowait(ruc)
    for _ in range(lane_count):
        feed.put_nowait(None)

    lane_cfg = LaneConfig(
        page_size=cfg.page_size,
        session_budget=cfg.session_budget,
        wait_min_s=cfg.wait_min_s,
        wait_max_s=cfg.wait_max_s,
        ban_cooldown_s=ban_cooldown_s,
    )
    lanes = [
        ProxyLane(
            run_id=run_id,
            lane_id=lane_id,
            slot_id=lane_id,
            provider=provider,
            store=store,
            cfg=lane_cfg,
        )
        for lane_id in range(1, lane_count + 1)
    ]

    async with asyncio.TaskGroup() as group:
        tasks = [group.create_task(lane.run(feed)) for lane in lanes]

    totals = LaneTotals()
    for task in tasks:
        lane_totals = task.result()
        totals.processed += lane_totals.processed
        totals.succeeded += lane_totals.succeeded
        totals.failed += lane_totals.failed
    return totals
