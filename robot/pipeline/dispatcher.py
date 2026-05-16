from __future__ import annotations

import logging
import multiprocessing as mp
import os
import queue

from dataclasses import dataclass
from threading import Thread
from typing import TYPE_CHECKING

from robot.domain.types import RUC, LookupResult, RunSummary
from robot.obs.logging import configure_logging, kv
from robot.pipeline.messages import ResultMessage, WorkerDoneMessage
from robot.pipeline.reader import ReadStats, enqueue_rucs
from robot.pipeline.settings import WorkerSettings
from robot.pipeline.worker import Worker
from robot.providers.geonode import load_geonode_config


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from robot.app.config import RunConfig
    from robot.io.writer import OutputWriter


@dataclass(frozen=True)
class WorkerProcess:
    worker_id: int
    process: mp.context.SpawnProcess


@dataclass(frozen=True)
class ProduceResult:
    read_stats: ReadStats
    error: BaseException | None = None


def _worker_entry(
    *,
    worker_id: int,
    run_id: str,
    geonode_env_file: str,
    task_queue: mp.JoinableQueue[RUC | None],
    result_queue: mp.Queue[ResultMessage | WorkerDoneMessage],
    settings: WorkerSettings,
) -> None:
    configure_logging(debug=settings.debug, run_id=run_id)
    geonode = load_geonode_config(env_file=geonode_env_file)

    class _QueueWriter:
        def write(self, result: LookupResult) -> None:
            result_queue.put(ResultMessage(result=result))

    worker = Worker(
        worker_id=worker_id,
        slot_id=worker_id,
        run_id=run_id,
        task_queue=task_queue,
        writer=_QueueWriter(),
        settings=settings,
        geonode=geonode,
    )
    summary = worker.run()
    result_queue.put(
        WorkerDoneMessage(
            worker_id=worker_id,
            processed=summary.processed,
            succeeded=summary.succeeded,
            failed=summary.failed,
        )
    )


def _build_settings(cfg: RunConfig) -> WorkerSettings:
    return WorkerSettings(
        page_size=cfg.page_size,
        session_budget=cfg.session_budget,
        wait_min_s=cfg.wait_min_s,
        wait_max_s=cfg.wait_max_s,
        ban_cooldown_s=cfg.ban_cooldown_s,
        captcha_timeout_s=cfg.captcha_timeout_s,
        captcha_timeout_first_s=cfg.captcha_timeout_first_s,
        captcha_same_session_retries=cfg.captcha_same_session_retries,
        captcha_first_token_jitter_max_s=cfg.captcha_first_token_jitter_max_s,
        chrome_binary=os.getenv("CHROME_BINARY", ""),
        debug=cfg.debug,
    )


def _start_producer(
    *,
    cfg: RunConfig,
    checkpoint: set[str],
    worker_count: int,
    task_queue: mp.JoinableQueue[RUC | None],
) -> tuple[Thread, dict[str, ProduceResult]]:
    holder: dict[str, ProduceResult] = {}

    def produce() -> None:
        try:
            stats = enqueue_rucs(
                cfg.input_csv,
                task_queue,
                dedupe=cfg.dedupe,
                checkpoint=checkpoint,
            )
            holder["result"] = ProduceResult(read_stats=stats)
        except BaseException as exc:  # noqa: BLE001
            holder["result"] = ProduceResult(read_stats=ReadStats(), error=exc)
        finally:
            for _ in range(worker_count):
                task_queue.put(None)

    thread = Thread(target=produce, daemon=True)
    thread.start()
    return thread, holder


def _start_workers(
    *,
    context: mp.context.SpawnContext,
    worker_count: int,
    run_id: str,
    env_file: str,
    task_queue: mp.JoinableQueue[RUC | None],
    result_queue: mp.Queue[ResultMessage | WorkerDoneMessage],
    settings: WorkerSettings,
) -> list[WorkerProcess]:
    entries: list[WorkerProcess] = []
    for idx in range(1, worker_count + 1):
        process = context.Process(
            target=_worker_entry,
            kwargs={
                "worker_id": idx,
                "run_id": run_id,
                "geonode_env_file": env_file,
                "task_queue": task_queue,
                "result_queue": result_queue,
                "settings": settings,
            },
            name=f"worker-{idx}",
        )
        process.start()
        entries.append(WorkerProcess(worker_id=idx, process=process))
    return entries


def _collect_results(
    *,
    worker_count: int,
    result_queue: mp.Queue[ResultMessage | WorkerDoneMessage],
    processes: list[WorkerProcess],
    writer: OutputWriter,
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

        if isinstance(item, WorkerDoneMessage):
            done += 1
            summary.processed += item.processed
            summary.succeeded += item.succeeded
            summary.failed += item.failed
            continue

        writer.write(item.result)

    return summary


def _join_workers(processes: list[WorkerProcess], *, timeout_s: float = 5.0) -> None:
    for entry in processes:
        entry.process.join(timeout=timeout_s)
    for entry in processes:
        if entry.process.is_alive():
            entry.process.terminate()
    for entry in processes:
        entry.process.join(timeout=timeout_s)


def run_dispatcher(
    cfg: RunConfig,
    *,
    writer: OutputWriter,
    checkpoint: set[str],
    run_id: str,
) -> RunSummary:
    settings = _build_settings(cfg)
    worker_count = cfg.workers
    context = mp.get_context("spawn")
    task_queue: mp.JoinableQueue[RUC | None] = context.JoinableQueue(
        maxsize=worker_count * 100
    )
    result_queue: mp.Queue[ResultMessage | WorkerDoneMessage] = context.Queue()

    producer, produce_holder = _start_producer(
        cfg=cfg,
        checkpoint=checkpoint,
        worker_count=worker_count,
        task_queue=task_queue,
    )
    processes = _start_workers(
        context=context,
        worker_count=worker_count,
        run_id=run_id,
        env_file=cfg.env_file,
        task_queue=task_queue,
        result_queue=result_queue,
        settings=settings,
    )
    summary = _collect_results(
        worker_count=worker_count,
        result_queue=result_queue,
        processes=processes,
        writer=writer,
    )
    producer.join()
    produce_result = produce_holder["result"]
    task_queue.join()
    _join_workers(processes)

    if produce_result.error is not None:
        msg = f"failed while reading input: {produce_result.error}"
        raise RuntimeError(msg)

    read_stats = produce_result.read_stats
    summary.rows_read = read_stats.rows_read
    summary.valid = read_stats.valid
    summary.ignored = read_stats.ignored
    summary.duplicates = read_stats.duplicates
    summary.skipped = read_stats.skipped
    if read_stats.valid == 0:
        msg = "no valid RUCs in input"
        raise RuntimeError(msg)

    return summary
