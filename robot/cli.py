from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal

from dataclasses import dataclass
from pathlib import Path

from robot.obs.events import RUN_START
from robot.obs.logging import configure_logging, kv, new_run_id
from robot.pipeline.run import run


@dataclass(frozen=True)
class RunConfig:
    input_csv: Path
    output_csv: Path
    page_size: int
    dedupe: bool
    debug: bool
    session_budget: int
    wait_min_s: float
    wait_max_s: float
    env_file: str


def parse_args(argv: list[str] | None = None) -> RunConfig:
    parser = argparse.ArgumentParser(prog="robot")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--page-size", type=int, default=5000)
    parser.add_argument("--dedupe", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--debug", action="store_true", default=False)
    # Session reuse is an OSIPTEL protocol constraint, not a proxy knob.
    parser.add_argument("--session-budget", type=int, default=1)
    parser.add_argument("--wait-min-s", type=float, default=0.0)
    parser.add_argument("--wait-max-s", type=float, default=0.0)
    parser.add_argument("--env-file", default=".env")
    ns = parser.parse_args(argv)

    errors: list[str] = []
    if ns.page_size < 1:
        errors.append("--page-size must be >= 1")
    if ns.session_budget < 1:
        errors.append("--session-budget must be >= 1")
    if ns.wait_min_s < 0:
        errors.append("--wait-min-s must be >= 0")
    if ns.wait_max_s < ns.wait_min_s:
        errors.append("--wait-max-s must be >= --wait-min-s")
    if errors:
        parser.error("; ".join(errors))

    return RunConfig(
        input_csv=ns.input,
        output_csv=ns.output,
        page_size=ns.page_size,
        dedupe=ns.dedupe,
        debug=ns.debug,
        session_budget=ns.session_budget,
        wait_min_s=ns.wait_min_s,
        wait_max_s=ns.wait_max_s,
        env_file=ns.env_file,
    )


def _raise_keyboard_interrupt(*_: object) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> None:
    cfg = parse_args(argv)
    run_id = new_run_id()

    configure_logging(debug=cfg.debug, run_id=run_id)
    logger = logging.getLogger(__name__)
    logger.info("%s %s", RUN_START, kv(run_id=run_id))

    # Detached runs receive SIGTERM; route it through the graceful cancellation
    # path so lanes release proxies and the durable store can resume.
    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    try:
        asyncio.run(run(cfg, run_id=run_id))
    except KeyboardInterrupt:
        logger.warning("run_interrupted %s", kv(run_id=run_id))


if __name__ == "__main__":
    main()
