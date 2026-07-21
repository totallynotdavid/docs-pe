from __future__ import annotations

import asyncio
import logging

from typing import TYPE_CHECKING

from fetch.domain.types import RunTotals
from fetch.obs.events import (
    PROVIDER_SELECTED,
    RUCS_UNROUTED,
    RUN_SUMMARY,
    SITE_SELECTED,
    SITE_SUMMARY,
)
from fetch.obs.logging import kv
from fetch.pipeline.breaker import CircuitBreaker
from fetch.pipeline.session import WorkerConfig
from fetch.pipeline.worker import run_worker
from fetch.proxy.load import load_proxy_providers
from fetch.store.export import export_all
from fetch.store.import_ import import_site
from fetch.store.outcomes import OutcomeStore, state_path_for_output
from fetch.store.plan import count_unrouted, plan_pending, read_rucs


if TYPE_CHECKING:
    from fetch.cli import RunConfig
    from fetch.domain.types import RUC, Site
    from fetch.proxy.base import ProxyProvider
    from fetch.store.plan import PlanCounts


logger = logging.getLogger(__name__)


async def run(cfg: RunConfig, *, run_id: str) -> None:
    sites = list(cfg.sites)
    providers = load_proxy_providers(env_file=cfg.env_file)

    with OutcomeStore(state_path_for_output(cfg.output_csv)) as store:
        if cfg.do_import:
            for site in sites:
                imported = import_site(
                    store=store, output_csv=cfg.output_csv, site=site
                )
                logger.info("site_import %s", kv(site=site.name, imported=imported))

        rucs, plan = read_rucs(cfg.input_csv, dedupe=cfg.dedupe)
        done = store.done_pairs()
        pending = plan_pending(rucs, sites, done)
        totals = {site.name: RunTotals() for site in sites}

        unrouted = count_unrouted(rucs, sites)
        if unrouted:
            logger.warning("%s %s", RUCS_UNROUTED, kv(run_id=run_id, unrouted=unrouted))

        try:
            for site in sites:
                logger.info(
                    "%s %s",
                    SITE_SELECTED,
                    kv(
                        run_id=run_id,
                        site=site.name,
                        pending=len(pending[site.name]),
                        session_budget=_budget(cfg, site),
                    ),
                )
            for provider in providers:
                logger.info(
                    "%s %s",
                    PROVIDER_SELECTED,
                    kv(
                        run_id=run_id,
                        provider=provider.name,
                        workers=_workers(cfg, provider),
                        ban_cooldown_s=_cooldown(cfg, provider),
                    ),
                )
            await _run_workers(
                cfg=cfg,
                store=store,
                sites=sites,
                providers=providers,
                pending=pending,
                run_id=run_id,
                totals=totals,
            )
        finally:
            # Export even on interruption, so artifacts on disk reflect the run.
            export_all(store=store, output_csv=cfg.output_csv, sites=sites)
            _log_summary(
                run_id=run_id,
                store=store,
                sites=sites,
                plan=plan,
                pending=pending,
                totals=totals,
                unrouted=unrouted,
            )


async def _run_workers(
    *,
    cfg: RunConfig,
    store: OutcomeStore,
    sites: list[Site],
    providers: list[ProxyProvider],
    pending: dict[str, list[RUC]],
    run_id: str,
    totals: dict[str, RunTotals],
) -> None:
    # Slots are allocated per provider across all sites, so GeoNode's slot->port map
    # never collides between sites sharing a provider.
    next_slot = {provider.name: 0 for provider in providers}

    async with asyncio.TaskGroup() as group:
        lane_id = 0
        for site in sites:
            site_pending = pending[site.name]
            if not site_pending:
                continue
            queue: asyncio.Queue[RUC] = asyncio.Queue()
            for ruc in site_pending:
                queue.put_nowait(ruc)
            budget = _budget(cfg, site)

            for provider in providers:
                # One breaker per (site, provider): a provider-wide outage parks
                # that site's lanes without stalling a healthy sibling.
                breaker = CircuitBreaker(
                    provider=f"{site.name}:{provider.name}", run_id=run_id
                )
                worker_cfg = WorkerConfig(
                    session_budget=budget,
                    wait_min_s=cfg.wait_min_s,
                    wait_max_s=cfg.wait_max_s,
                    ban_cooldown_s=_cooldown(cfg, provider),
                )
                for _ in range(_workers(cfg, provider)):
                    next_slot[provider.name] += 1
                    lane_id += 1
                    group.create_task(
                        run_worker(
                            queue=queue,
                            site=site,
                            store=store,
                            provider=provider,
                            breaker=breaker,
                            slot_id=next_slot[provider.name],
                            lane_id=lane_id,
                            run_id=run_id,
                            cfg=worker_cfg,
                            totals=totals[site.name],
                        )
                    )


def _budget(cfg: RunConfig, site: Site) -> int:
    if cfg.session_budget is not None:
        return cfg.session_budget
    return site.tuning.session_budget


def _workers(cfg: RunConfig, provider: ProxyProvider) -> int:
    if cfg.workers is not None:
        return cfg.workers
    return provider.tuning.workers


def _cooldown(cfg: RunConfig, provider: ProxyProvider) -> float:
    if cfg.ban_cooldown_s is not None:
        return cfg.ban_cooldown_s
    return provider.tuning.ban_cooldown_s


def _log_summary(
    *,
    run_id: str,
    store: OutcomeStore,
    sites: list[Site],
    plan: PlanCounts,
    pending: dict[str, list[RUC]],
    totals: dict[str, RunTotals],
    unrouted: int,
) -> None:
    total_pending = total_processed = total_succeeded = total_failed = 0
    total_not_found = 0
    overall_succeeded = overall_not_found = overall_terminal = overall_retryable = 0

    for site in sites:
        counts = store.counts(site.name)
        totals_for_site = totals[site.name]
        site_pending = len(pending[site.name])
        logger.info(
            "%s %s",
            SITE_SUMMARY,
            kv(
                run_id=run_id,
                site=site.name,
                pending=site_pending,
                processed=totals_for_site.processed,
                succeeded=totals_for_site.succeeded,
                not_found=totals_for_site.not_found,
                failed=totals_for_site.failed,
                total_succeeded=counts.succeeded,
                total_not_found=counts.not_found,
                total_terminal_failed=counts.terminal_failed,
                total_retryable=counts.retryable,
            ),
        )
        total_pending += site_pending
        total_processed += totals_for_site.processed
        total_succeeded += totals_for_site.succeeded
        total_not_found += totals_for_site.not_found
        total_failed += totals_for_site.failed
        overall_succeeded += counts.succeeded
        overall_not_found += counts.not_found
        overall_terminal += counts.terminal_failed
        overall_retryable += counts.retryable

    logger.info(
        "%s %s",
        RUN_SUMMARY,
        kv(
            run_id=run_id,
            state_db=store.path,
            sites=",".join(site.name for site in sites),
            rows_read=plan.rows_read,
            valid=plan.valid,
            ignored=plan.ignored,
            duplicates=plan.duplicates,
            unrouted=unrouted,
            pending=total_pending,
            processed=total_processed,
            succeeded=total_succeeded,
            not_found=total_not_found,
            failed=total_failed,
            total_succeeded=overall_succeeded,
            total_not_found=overall_not_found,
            total_terminal_failed=overall_terminal,
            total_retryable=overall_retryable,
        ),
    )
