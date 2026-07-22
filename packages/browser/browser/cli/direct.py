from __future__ import annotations

import argparse

from pathlib import Path

from browser.run import RunConfig, run
from browser.sites.registry import SITES
from browser.subject import Subject


def parse_args(argv: list[str] | None = None) -> RunConfig:
    parser = argparse.ArgumentParser(
        prog="browser",
        description="Collect site data through SeleniumBase CDP under Xvfb",
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--site", required=True, choices=sorted(SITES))
    parser.add_argument(
        "--control",
        help="warm-up identifier for sites that need one (e.g. Entel); "
        "must be an identifier the site accepts. Portabilidad ignores it.",
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
        help="route Chrome through the configured proxy (default: enabled); "
        "pass --no-proxy for a direct local run",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="env file with PROXY_PROVIDER and provider credentials (default: .env)",
    )
    parser.add_argument(
        "--diagnostics",
        type=Path,
        help="append redacted browser and request diagnostics as JSON Lines",
    )
    parser.add_argument("--max-session-restarts", type=int, default=0)
    parser.add_argument(
        "--reject-retries",
        type=int,
        default=12,
        help="re-mint a fresh token this many extra times when a lookup is "
        "rejected (a fluctuating verdict clears only some of the time)",
    )
    parser.add_argument(
        "--reject-restart-threshold",
        type=int,
        default=4,
        help="restart the browser session after this many subjects in a row "
        "exhaust their retries (likely a degraded window)",
    )
    args = parser.parse_args(argv)

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
            if not SITES[args.site].accepts(control):
                errors.append(f"--control is not served by site {args.site}")
    if errors:
        parser.error("; ".join(errors))

    state = args.state or args.output.with_suffix(".state.sqlite3")
    return RunConfig(
        input_csv=args.input,
        output_csv=args.output,
        state_db=state,
        site=args.site,
        control=args.control,
        software_webgl=args.software_webgl,
        diagnostics=args.diagnostics,
        max_session_restarts=args.max_session_restarts,
        env_file=args.env_file,
        use_proxy=args.proxy,
        reject_retries=args.reject_retries,
        reject_restart_threshold=args.reject_restart_threshold,
    )


def main(argv: list[str] | None = None) -> None:
    config = parse_args(argv)
    raise SystemExit(run(config, SITES[config.site]))


if __name__ == "__main__":
    main()
