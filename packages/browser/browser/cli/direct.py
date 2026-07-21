from __future__ import annotations

import argparse
import shutil

from pathlib import Path

from browser.ruc import RUC
from browser.run import RunConfig, run
from browser.sites.registry import SITES


DEFAULT_CONTROL_RUC = "20610448187"
DEFAULT_BROWSER_BINARY = Path(shutil.which("google-chrome") or "/usr/bin/google-chrome")
DEFAULT_SITE = "entel"


def parse_args(argv: list[str] | None = None) -> RunConfig:
    parser = argparse.ArgumentParser(
        prog="browser",
        description="Collect site data through direct Google Chrome over CDP",
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--site", default=DEFAULT_SITE, choices=sorted(SITES))
    parser.add_argument("--control-ruc", default=DEFAULT_CONTROL_RUC)
    parser.add_argument("--binary", type=Path, default=DEFAULT_BROWSER_BINARY)
    parser.add_argument(
        "--software-webgl",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="use SwiftShader on the virtual display (default: enabled)",
    )
    parser.add_argument(
        "--diagnostics",
        type=Path,
        help="append redacted browser and request diagnostics as JSON Lines",
    )
    parser.add_argument("--display", type=int)
    parser.add_argument("--max-session-restarts", type=int, default=0)
    parser.add_argument(
        "--reject-retries",
        type=int,
        default=12,
        help="re-mint a fresh token this many extra times when a lookup is "
        "rejected (the v3 score fluctuates, so ~half of single mints clear)",
    )
    parser.add_argument(
        "--reject-restart-threshold",
        type=int,
        default=4,
        help="restart the browser session after this many RUCs in a row "
        "exhaust their retries (likely a degraded score window)",
    )
    args = parser.parse_args(argv)

    errors: list[str] = []
    if not args.input.exists():
        errors.append(f"--input file not found: {args.input}")
    if not args.binary.is_file():
        errors.append(f"--binary file not found: {args.binary}")
    if args.display is not None and args.display < 1:
        errors.append("--display must be >= 1")
    if args.max_session_restarts < 0:
        errors.append("--max-session-restarts must be >= 0")
    if args.reject_retries < 0:
        errors.append("--reject-retries must be >= 0")
    if args.reject_restart_threshold < 1:
        errors.append("--reject-restart-threshold must be >= 1")
    try:
        RUC(args.control_ruc)
    except ValueError:
        errors.append("--control-ruc must be an 11-digit RUC")
    if errors:
        parser.error("; ".join(errors))

    state = args.state or args.output.with_suffix(".state.sqlite3")
    profile = args.output.with_name(f".{args.output.stem}.{args.site}-chrome")
    return RunConfig(
        input_csv=args.input,
        output_csv=args.output,
        state_db=state,
        site=args.site,
        profile=profile,
        control_ruc=args.control_ruc,
        binary=args.binary.resolve(),
        software_webgl=args.software_webgl,
        diagnostics=args.diagnostics,
        display=args.display,
        max_session_restarts=args.max_session_restarts,
        reject_retries=args.reject_retries,
        reject_restart_threshold=args.reject_restart_threshold,
    )


def main(argv: list[str] | None = None) -> None:
    config = parse_args(argv)
    raise SystemExit(run(config, SITES[config.site]))


if __name__ == "__main__":
    main()
