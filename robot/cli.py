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
    # Optional provider-tuning overrides. None means "use the selected provider's
    # recommended default" (resolved in robot.pipeline.run against tuning).
    workers: int | None
    ban_cooldown_s: float | None
    env_file: str


def parse_args(argv: list[str] | None = None) -> RunConfig:
    parser = argparse.ArgumentParser(prog="robot")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--page-size", type=int, default=5000)
    # --workers and --ban-cooldown-s default to None: the selected provider's
    # ProviderTuning supplies the baseline (GeoNode measured ~15 workers; see
    # robot/providers/*.py), and a flag here overrides it for ad-hoc tuning.
    # Throughput scales near-linearly with workers and memory stays flat, so
    # workers are bounded by the proxy gateway, not the host.
    parser.add_argument("--workers", type=int, default=None)
    parser.add_argument("--dedupe", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--debug", action="store_true", default=False)
    # Keep --session-budget at 1. OSIPTEL requires a fresh home-page warmup
    # before each lookup; reusing a session for a second API call triggers WAF
    # blocks (status=500). Budget>1 trades a smaller session count for a high
    # ban rate, so it consumes fewer proxy sessions but fails most lookups. This
    # is an OSIPTEL invariant, not a proxy knob, so it stays provider-agnostic.
    parser.add_argument("--session-budget", type=int, default=1)
    parser.add_argument("--wait-min-s", type=float, default=0.0)
    parser.add_argument("--wait-max-s", type=float, default=0.0)
    parser.add_argument("--ban-cooldown-s", type=float, default=None)
    parser.add_argument("--env-file", default=".env")
    ns = parser.parse_args(argv)

    errors: list[str] = []
    if ns.page_size < 1:
        errors.append("--page-size must be >= 1")
    if ns.workers is not None and ns.workers < 1:
        errors.append("--workers must be >= 1")
    if ns.session_budget < 1:
        errors.append("--session-budget must be >= 1")
    if ns.wait_min_s < 0:
        errors.append("--wait-min-s must be >= 0")
    if ns.wait_max_s < ns.wait_min_s:
        errors.append("--wait-max-s must be >= --wait-min-s")
    if ns.ban_cooldown_s is not None and ns.ban_cooldown_s < 0:
        errors.append("--ban-cooldown-s must be >= 0")
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
        workers=ns.workers,
        ban_cooldown_s=ns.ban_cooldown_s,
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

    # Translate SIGTERM (detached runs are stopped this way) into the same
    # graceful cancellation path as Ctrl-C; lanes release their proxies in their
    # finally blocks and the durable store lets a restart resume.
    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    try:
        asyncio.run(run(cfg, run_id=run_id))
    except KeyboardInterrupt:
        logger.warning("run_interrupted %s", kv(run_id=run_id))


if __name__ == "__main__":
    main()
