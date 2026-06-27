from __future__ import annotations

import asyncio
import logging

from typing import TYPE_CHECKING

from robot.domain.types import RunReport, RunTotals
from robot.obs.events import PROVIDER_SELECTED, RUN_SUMMARY
from robot.obs.logging import kv
from robot.pipeline.lane import run_lane
from robot.pipeline.session import LaneConfig
from robot.providers.proxy import load_proxy_providers
from robot.store.export import export_csv
from robot.store.outcome_log import OutcomeLog, state_path_for_output
from robot.store.plan import derive_pending


if TYPE_CHECKING:
    from robot.cli import RunConfig
    from robot.domain.types import RUC
    from robot.providers.proxy import ProxyProvider


logger = logging.getLogger(__name__)


async def run(cfg: RunConfig, *, run_id: str) -> RunReport:
    store_path = state_path_for_output(cfg.output_csv)

    with OutcomeLog(store_path) as log:
        done = log.done_rucs()
        if done:
            seeded = 0
        else:
            seeded = log.import_csv(cfg.output_csv)
            done = log.done_rucs()

        pending, plan = derive_pending(
            input_csv=cfg.input_csv, done=done, dedupe=cfg.dedupe
        )

        totals = RunTotals()
        try:
            if pending:
                providers = load_proxy_providers(env_file=cfg.env_file)
                for provider in providers:
                    logger.info(
                        "%s %s",
                        PROVIDER_SELECTED,
                        kv(
                            run_id=run_id,
                            provider=provider.name,
                            workers=provider.tuning.workers,
                            ban_cooldown_s=provider.tuning.ban_cooldown_s,
                            pending=plan.pending,
                        ),
                    )
                await _run_lanes(
                    cfg=cfg,
                    log=log,
                    providers=providers,
                    pending=pending,
                    run_id=run_id,
                    totals=totals,
                )
        finally:
            # Always export in finally so interrupted runs leave CSV artifacts.
            export_csv(log=log, output_csv=cfg.output_csv)
            counts = log.counts()

        report = RunReport(
            rows_read=plan.rows_read,
            valid=plan.valid,
            ignored=plan.ignored,
            duplicates=plan.duplicates,
            already_done=plan.already_done,
            seeded=seeded,
            pending=plan.pending,
            processed=totals.processed,
            succeeded=totals.succeeded,
            failed=totals.failed,
            remaining=plan.pending - totals.processed,
            total_succeeded=counts.succeeded,
            total_failed=counts.failed,
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
            already_done=report.already_done,
            seeded=report.seeded,
            pending=report.pending,
            processed=report.processed,
            succeeded=report.succeeded,
            failed=report.failed,
            remaining=report.remaining,
            total_succeeded=report.total_succeeded,
            total_failed=report.total_failed,
        ),
    )
    return report


async def _run_lanes(
    *,
    cfg: RunConfig,
    log: OutcomeLog,
    providers: list[ProxyProvider],
    pending: list[RUC],
    run_id: str,
    totals: RunTotals,
) -> None:
    lane_cfg = LaneConfig(
        page_size=cfg.page_size,
        session_budget=cfg.session_budget,
        wait_min_s=cfg.wait_min_s,
        wait_max_s=cfg.wait_max_s,
    )
    queue: asyncio.Queue[RUC] = asyncio.Queue()
    for ruc in pending:
        queue.put_nowait(ruc)

    async with asyncio.TaskGroup() as group:
        lane_id = 0
        for provider in providers:
            for slot_id in range(1, provider.tuning.workers + 1):
                lane_id += 1
                group.create_task(
                    run_lane(
                        queue=queue,
                        log=log,
                        provider=provider,
                        slot_id=slot_id,
                        lane_id=lane_id,
                        run_id=run_id,
                        cfg=lane_cfg,
                        totals=totals,
                    )
                )
