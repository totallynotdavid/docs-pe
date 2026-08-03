from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from fetch.obs.events import RUN_START
from fetch.obs.logging import configure_logging, kv, new_run_id
from fetch.pipeline.run import run
from fetch.sites.registry import SITES, get_sites


if TYPE_CHECKING:
    from fetch.domain.types import Site


@dataclass(frozen=True)
class RunConfig:
    input_csv: Path
    output_csv: Path
    sites: tuple[Site, ...]
    dedupe: bool
    debug: bool
    # None means "use each site's or provider's own default". Lane count is per
    # provider and lives in PROXY_PROVIDER (e.g. "geonode:30,dataimpulse:18").
    session_budget: int | None
    ban_cooldown_s: float | None
    wait_min_s: float
    wait_max_s: float
    env_file: str
    do_import: bool


def parse_args(argv: list[str] | None = None) -> RunConfig:
    parser = argparse.ArgumentParser(prog="fetch")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument(
        "--sites", required=True, help="comma-separated: " + ",".join(sorted(SITES))
    )
    parser.add_argument("--dedupe", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--debug", action="store_true", default=False)
    parser.add_argument("--session-budget", type=int, default=None)
    parser.add_argument("--ban-cooldown-s", type=float, default=None)
    parser.add_argument("--wait-min-s", type=float, default=0.0)
    parser.add_argument("--wait-max-s", type=float, default=0.0)
    parser.add_argument("--env-file", default=".env")
    # Opt-in recovery: rebuild the store from prior per-site exports before planning.
    parser.add_argument(
        "--import", dest="do_import", action="store_true", default=False
    )
    ns = parser.parse_args(argv)

    site_names = [
        chunk.strip().lower() for chunk in ns.sites.split(",") if chunk.strip()
    ]

    errors: list[str] = []
    if not ns.input.exists():
        errors.append(f"--input file not found: {ns.input}")
    sites: tuple[Site, ...] = ()
    try:
        sites = tuple(get_sites(site_names))
    except ValueError as exc:
        errors.append(f"--sites {exc}")
    if ns.session_budget is not None and ns.session_budget < 1:
        errors.append("--session-budget must be >= 1")
    if ns.ban_cooldown_s is not None and ns.ban_cooldown_s < 0:
        errors.append("--ban-cooldown-s must be >= 0")
    if ns.wait_min_s < 0:
        errors.append("--wait-min-s must be >= 0")
    if ns.wait_max_s < ns.wait_min_s:
        errors.append("--wait-max-s must be >= --wait-min-s")
    if errors:
        parser.error("; ".join(errors))

    return RunConfig(
        input_csv=ns.input,
        output_csv=ns.output,
        sites=sites,
        dedupe=ns.dedupe,
        debug=ns.debug,
        session_budget=ns.session_budget,
        ban_cooldown_s=ns.ban_cooldown_s,
        wait_min_s=ns.wait_min_s,
        wait_max_s=ns.wait_max_s,
        env_file=ns.env_file,
        do_import=ns.do_import,
    )


def _raise_keyboard_interrupt(*_: object) -> None:
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> None:
    cfg = parse_args(argv)
    run_id = new_run_id()

    configure_logging(debug=cfg.debug, run_id=run_id)
    logger = logging.getLogger(__name__)
    logger.info(
        "%s %s",
        RUN_START,
        kv(run_id=run_id, sites=",".join(site.name for site in cfg.sites)),
    )

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
