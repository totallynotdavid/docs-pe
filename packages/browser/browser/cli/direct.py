from __future__ import annotations

import argparse

from pathlib import Path

from browser.run import RunConfig, run
from browser.sites.registry import SITES
from browser.subject import Subject


def parse_args(argv: list[str] | None = None) -> RunConfig:
    parser = argparse.ArgumentParser(
        prog="browser",
        description="Collect site data through Chrome over CDP",
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--site", required=True, choices=sorted(SITES))
    parser.add_argument(
        "--control",
        help="warm-up identifier for sites that require one",
    )
    parser.add_argument(
        "--software-webgl",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="use SwiftShader on the virtual display (default: enabled)",
    )
    parser.add_argument(
        "--proxy",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="use the configured proxy (default: enabled)",
    )
    parser.add_argument(
        "--diagnostics",
        type=Path,
        help="append redacted diagnostics as JSON Lines",
    )
    parser.add_argument("--max-session-restarts", type=int, default=0)
    parser.add_argument(
        "--reject-retries",
        type=int,
        default=12,
        help="extra token mints after a rejected lookup",
    )
    parser.add_argument(
        "--reject-restart-threshold",
        type=int,
        default=4,
        help="restart after this many consecutive exhausted subjects",
    )

    args = parser.parse_args(argv)
    site = SITES[args.site]

    errors: list[str] = []

    if not args.input.exists():
        errors.append(f"--input file not found: {args.input}")

    if args.max_session_restarts < 0:
        errors.append("--max-session-restarts must be >= 0")

    if args.reject_retries < 0:
        errors.append("--reject-retries must be >= 0")

    if args.reject_restart_threshold < 1:
        errors.append("--reject-restart-threshold must be >= 1")

    if args.control is not None:
        try:
            control = Subject(args.control)
        except ValueError as exc:
            errors.append(f"--control is not a valid identifier: {exc}")
        else:
            if not site.accepts(control):
                errors.append(f"--control is not served by site {args.site}")

    if errors:
        parser.error("; ".join(errors))

    return RunConfig(
        input_csv=args.input,
        output_csv=args.output,
        state_db=args.state or args.output.with_suffix(".state.sqlite3"),
        site=args.site,
        control=args.control,
        software_webgl=args.software_webgl,
        diagnostics=args.diagnostics,
        max_session_restarts=args.max_session_restarts,
        use_proxy=args.proxy,
        reject_retries=args.reject_retries,
        reject_restart_threshold=args.reject_restart_threshold,
    )


def main(argv: list[str] | None = None) -> None:
    config = parse_args(argv)
    raise SystemExit(run(config, SITES[config.site]))


if __name__ == "__main__":
    main()
