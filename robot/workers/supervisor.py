from __future__ import annotations

import logging
import multiprocessing as mp
import queue

from dataclasses import dataclass

from robot.domain.types import RunSummary
from robot.obs.logging import configure_logging, kv
from robot.providers.geonode import load_geonode_config
from robot.workers.runner import DurableWorker, WorkerRuntimeConfig


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerDoneMessage:
    worker_id: int
    processed: int
    succeeded: int
    failed: int


@dataclass(frozen=True)
class WorkerProcess:
    worker_id: int
    process: mp.context.SpawnProcess


def run_workers(
    *,
    run_id: str,
    store_path: str,
    env_file: str,
    worker_count: int,
    page_size: int,
    session_budget: int,
    wait_min_s: float,
    wait_max_s: float,
    ban_cooldown_s: float,
    debug: bool,
) -> RunSummary:
    context = mp.get_context("spawn")
    result_queue: mp.Queue[WorkerDoneMessage] = context.Queue()
    processes = _start_workers(
        context=context,
        result_queue=result_queue,
        run_id=run_id,
        store_path=store_path,
        env_file=env_file,
        worker_count=worker_count,
        page_size=page_size,
        session_budget=session_budget,
        wait_min_s=wait_min_s,
        wait_max_s=wait_max_s,
        ban_cooldown_s=ban_cooldown_s,
        debug=debug,
    )
    try:
        return _collect_results(
            worker_count=worker_count,
            result_queue=result_queue,
            processes=processes,
        )
    finally:
        _join_workers(processes)


def _worker_entry(
    *,
    cfg: WorkerRuntimeConfig,
    env_file: str,
    result_queue: mp.Queue[WorkerDoneMessage],
    debug: bool,
) -> None:
    configure_logging(debug=debug, run_id=cfg.run_id)
    geonode = load_geonode_config(env_file=env_file)
    summary = DurableWorker(cfg=cfg, geonode=geonode).run()
    result_queue.put(
        WorkerDoneMessage(
            worker_id=cfg.worker_id,
            processed=summary.processed,
            succeeded=summary.succeeded,
            failed=summary.failed,
        )
    )


def _start_workers(
    *,
    context: mp.context.SpawnContext,
    result_queue: mp.Queue[WorkerDoneMessage],
    run_id: str,
    store_path: str,
    env_file: str,
    worker_count: int,
    page_size: int,
    session_budget: int,
    wait_min_s: float,
    wait_max_s: float,
    ban_cooldown_s: float,
    debug: bool,
) -> list[WorkerProcess]:
    processes: list[WorkerProcess] = []
    for worker_id in range(1, worker_count + 1):
        cfg = WorkerRuntimeConfig(
            run_id=run_id,
            worker_id=worker_id,
            store_path=store_path,
            page_size=page_size,
            session_budget=session_budget,
            wait_min_s=wait_min_s,
            wait_max_s=wait_max_s,
            ban_cooldown_s=ban_cooldown_s,
        )
        process = context.Process(
            target=_worker_entry,
            kwargs={
                "cfg": cfg,
                "env_file": env_file,
                "result_queue": result_queue,
                "debug": debug,
            },
            name=f"worker-{worker_id}",
        )
        process.start()
        processes.append(WorkerProcess(worker_id=worker_id, process=process))
    return processes


def _collect_results(
    *,
    worker_count: int,
    result_queue: mp.Queue[WorkerDoneMessage],
    processes: list[WorkerProcess],
) -> RunSummary:
    summary = RunSummary()
    done = 0
    reported_dead: set[int] = set()
    while done < worker_count:
        for entry in processes:
            if entry.worker_id in reported_dead:
                continue
            exit_code = entry.process.exitcode
            if exit_code is None:
                continue
            if exit_code != 0:
                logger.warning(
                    "worker_process_exited %s",
                    kv(worker_id=entry.worker_id, exit_code=exit_code),
                )
            reported_dead.add(entry.worker_id)

        try:
            item = result_queue.get(timeout=1.0)
        except queue.Empty:
            if any(entry.process.is_alive() for entry in processes):
                continue
            states = ",".join(
                f"worker={entry.worker_id}:exit_code={entry.process.exitcode}"
                for entry in processes
            )
            msg = f"worker exited unexpectedly before sending summary states={states}"
            raise RuntimeError(msg) from None

        done += 1
        summary.processed += item.processed
        summary.succeeded += item.succeeded
        summary.failed += item.failed

    return summary


def _join_workers(processes: list[WorkerProcess], *, timeout_s: float = 5.0) -> None:
    for entry in processes:
        entry.process.join(timeout=timeout_s)
    for entry in processes:
        if entry.process.is_alive():
            entry.process.terminate()
    for entry in processes:
        entry.process.join(timeout=timeout_s)
