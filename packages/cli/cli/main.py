from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from core.obs.events import RUN_START
from core.obs.logging import kv
from core.sites.registry import SITES, get_sites

from cli.logging import configure_logging, new_run_id
from cli.pipeline.run import run


if TYPE_CHECKING:
    from core.domain.types import Site


@dataclass(frozen=True)
class RunConfig:
    input_csv: Path
    output_csv: Path
    sites: tuple[Site, ...]
    dedupe: bool
    debug: bool
    session_budget: int | None
    ban_cooldown_s: float | None
    wait_min_s: float
    wait_max_s: float
    do_import: bool


def parse_args(argv: list[str] | None = None) -> RunConfig:
    parser = argparse.ArgumentParser(prog="fetch")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--sites",
        required=True,
        help="comma-separated: " + ",".join(sorted(SITES)),
    )
    parser.add_argument(
        "--dedupe",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="drop duplicate documents from the input (default: enabled)",
    )
    parser.add_argument("--debug", action="store_true", default=False)
    parser.add_argument(
        "--session-budget",
        type=int,
        default=None,
        help=(
            "lookups per sticky session; a site's own value is a ceiling and "
            "this can only lower it (default: the site's own default)"
        ),
    )
    parser.add_argument(
        "--ban-cooldown-s",
        type=float,
        default=None,
        help="delay after a provider ban (default: the provider's own default)",
    )
    parser.add_argument(
        "--wait-min-s",
        type=float,
        default=0.0,
        help="minimum delay after a successful lookup (default: %(default)s)",
    )
    parser.add_argument(
        "--wait-max-s",
        type=float,
        default=0.0,
        help="maximum delay, sampled uniformly (default: %(default)s)",
    )
    parser.add_argument(
        "--import",
        dest="do_import",
        action="store_true",
        default=False,
        help="rebuild state from prior per-site exports before planning",
    )

    args = parser.parse_args(argv)

    site_names = [
        chunk.strip().lower() for chunk in args.sites.split(",") if chunk.strip()
    ]

    errors: list[str] = []

    if not args.input.exists():
        errors.append(f"--input file not found: {args.input}")

    sites: tuple[Site, ...] = ()

    try:
        sites = tuple(get_sites(site_names))
    except ValueError as exc:
        errors.append(f"--sites {exc}")

    if args.session_budget is not None and args.session_budget < 1:
        errors.append("--session-budget must be >= 1")

    if args.ban_cooldown_s is not None and args.ban_cooldown_s < 0:
        errors.append("--ban-cooldown-s must be >= 0")

    if args.wait_min_s < 0:
        errors.append("--wait-min-s must be >= 0")

    if args.wait_max_s < args.wait_min_s:
        errors.append("--wait-max-s must be >= --wait-min-s")

    if errors:
        parser.error("; ".join(errors))

    return RunConfig(
        input_csv=args.input,
        output_csv=args.output,
        sites=sites,
        dedupe=args.dedupe,
        debug=args.debug,
        session_budget=args.session_budget,
        ban_cooldown_s=args.ban_cooldown_s,
        wait_min_s=args.wait_min_s,
        wait_max_s=args.wait_max_s,
        do_import=args.do_import,
    )


def _raise_keyboard_interrupt(*_: object) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> None:
    config = parse_args(argv)
    run_id = new_run_id()

    configure_logging(debug=config.debug, run_id=run_id)

    logger = logging.getLogger(__name__)
    logger.info(
        "%s %s",
        RUN_START,
        kv(
            run_id=run_id,
            sites=",".join(site.name for site in config.sites),
        ),
    )

    # Route SIGTERM through the normal cancellation path.
    with contextlib.suppress(ValueError, OSError):
        signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    try:
        asyncio.run(run(config, run_id=run_id))
    except KeyboardInterrupt:
        logger.warning("run_interrupted %s", kv(run_id=run_id))


if __name__ == "__main__":
    main()
