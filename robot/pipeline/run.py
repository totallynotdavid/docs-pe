from __future__ import annotations

import asyncio
import logging

from typing import TYPE_CHECKING

from robot.domain.types import LaneTotals, RunReport
from robot.jobs.exporter import export_csv
from robot.jobs.planner import plan_jobs
from robot.jobs.store import JobStore, state_path_for_output
from robot.obs.events import LEASE_RECLAIMED, PROVIDER_SELECTED, RUN_SUMMARY
from robot.obs.logging import kv
from robot.pipeline.lane import LaneConfig, ProxyLane
from robot.providers.proxy import load_proxy_providers


if TYPE_CHECKING:
    from robot.cli import RunConfig
    from robot.providers.proxy import ProxyProvider


logger = logging.getLogger(__name__)

# The lease must exceed a complete lookup across all attempts. Expiration means
# the owner is treated as dead and the reaper may reclaim the job.
LEASE_S = 600.0
REAPER_INTERVAL_S = 120.0


async def run(cfg: RunConfig, *, run_id: str) -> RunReport:
    store_path = state_path_for_output(cfg.output_csv)
    state_existed = store_path.exists()

    with JobStore(store_path) as store:
        seeded = 0 if state_existed else store.seed_success_csv(cfg.output_csv)
        plan = plan_jobs(input_csv=cfg.input_csv, store=store, dedupe=cfg.dedupe)
        # Startup recovery only moves expired leases, so peer processes can keep
        # live leases on the same DB.
        reclaimed = store.reset_expired_leases()
        if reclaimed:
            logger.info(
                "%s %s",
                LEASE_RECLAIMED,
                kv(run_id=run_id, reclaimed=reclaimed, phase="startup"),
            )
        remaining = store.summary().pending

        totals = LaneTotals()
        try:
            if remaining:
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
                            pending=remaining,
                        ),
                    )
                totals = await _drive_lanes(
                    cfg=cfg,
                    store=store,
                    providers=providers,
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
            in_progress=store_summary.in_progress,
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
            in_progress=report.in_progress,
            failed_jobs=report.failed_jobs,
        ),
    )
    return report


async def _drive_lanes(
    *,
    cfg: RunConfig,
    store: JobStore,
    providers: list[ProxyProvider],
    run_id: str,
) -> LaneTotals:
    lane_cfg = LaneConfig(
        page_size=cfg.page_size,
        session_budget=cfg.session_budget,
        wait_min_s=cfg.wait_min_s,
        wait_max_s=cfg.wait_max_s,
        lease_s=LEASE_S,
    )
    # Providers contribute measured lane counts to one shared queue. slot_id is
    # provider-local; lane_id is global for logging and lease ownership.
    lanes: list[ProxyLane] = []
    lane_id = 0
    for provider in providers:
        for slot_id in range(1, provider.tuning.workers + 1):
            lane_id += 1
            lanes.append(
                ProxyLane(
                    run_id=run_id,
                    lane_id=lane_id,
                    slot_id=slot_id,
                    provider=provider,
                    store=store,
                    cfg=lane_cfg,
                )
            )

    stop = asyncio.Event()
    reaper = asyncio.create_task(
        _reap_expired_leases(store=store, stop=stop, run_id=run_id)
    )
    tasks: list[asyncio.Task[LaneTotals]] = []
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(lane.run()) for lane in lanes]
    finally:
        stop.set()
        await reaper

    totals = LaneTotals()
    for task in tasks:
        lane_totals = task.result()
        totals.processed += lane_totals.processed
        totals.succeeded += lane_totals.succeeded
        totals.failed += lane_totals.failed
    return totals


async def _reap_expired_leases(
    *, store: JobStore, stop: asyncio.Event, run_id: str
) -> None:
    """Periodically requeue jobs whose owning lane died mid-lookup.

    Runs alongside the lanes so a crashed peer process is recovered without
    waiting for the next launch. Exits as soon as the lanes are done.
    """
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=REAPER_INTERVAL_S)
        except TimeoutError:
            reclaimed = store.reset_expired_leases()
            if reclaimed:
                logger.info(
                    "%s %s",
                    LEASE_RECLAIMED,
                    kv(run_id=run_id, reclaimed=reclaimed, phase="reaper"),
                )
