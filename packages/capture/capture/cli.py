from __future__ import annotations

import argparse
import secrets
import sys

from dataclasses import dataclass
from pathlib import Path

from capture.diagnostics import DiagnosticLog
from capture.ingest import new_run_id, read_rucs
from capture.relay import RelayState, build_server, write_browser_script
from capture.sites.registry import SITES
from capture.store import ObservationStore


DEFAULT_SITE = "entel"


@dataclass(frozen=True)
class CaptureConfig:
    input_csv: Path
    output_csv: Path
    state_db: Path
    site: str
    browser_script: Path
    diagnostics: Path | None
    port: int


def parse_args(argv: list[str] | None = None) -> CaptureConfig:
    parser = argparse.ArgumentParser(
        prog="capture",
        description="Collect site data through your own reputable Chrome session",
    )
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--site", default=DEFAULT_SITE, choices=sorted(SITES))
    parser.add_argument("--browser-script", type=Path)
    parser.add_argument(
        "--diagnostics",
        type=Path,
        help="append redacted browser and request diagnostics as JSON Lines",
    )
    parser.add_argument("--port", type=int, default=8765)

    args = parser.parse_args(argv)

    errors: list[str] = []

    if not args.input.exists():
        errors.append(f"--input file not found: {args.input}")

    if not 1024 <= args.port <= 65535:
        errors.append("--port must be between 1024 and 65535")

    if errors:
        parser.error("; ".join(errors))

    state_db = args.state or args.output.with_suffix(".state.sqlite3")
    browser_script = args.browser_script or args.output.with_suffix(
        f".{args.site}-capture.js"
    )

    return CaptureConfig(
        input_csv=args.input,
        output_csv=args.output,
        state_db=state_db,
        site=args.site,
        browser_script=browser_script,
        diagnostics=args.diagnostics,
        port=args.port,
    )


def run(config: CaptureConfig) -> int:
    site = SITES[config.site]
    rucs, counts = read_rucs(config.input_csv, dedupe=True)

    if not rucs:
        print("No valid RUCs found in input.", file=sys.stderr)
        return 2

    token = secrets.token_urlsafe(32)
    run_id = new_run_id()
    relay_url = f"http://127.0.0.1:{config.port}"

    diagnostic_log = (
        DiagnosticLog(
            config.diagnostics,
            run_id=run_id,
            source="reputable-chrome",
        )
        if config.diagnostics is not None
        else None
    )

    write_browser_script(
        destination=config.browser_script,
        relay_url=relay_url,
        token=token,
        site=site,
    )

    with ObservationStore(config.state_db) as store:
        relay_state = RelayState(
            rucs=rucs,
            store=store,
            run_id=run_id,
            token=token,
            site=site,
            diagnostic_log=diagnostic_log,
        )
        server = build_server(
            host="127.0.0.1",
            port=config.port,
            state=relay_state,
        )

        print(
            f"{site.name} run {run_id}: {len(rucs)} RUCs "
            f"({counts.ignored} invalid/blank rows ignored)",
            flush=True,
        )
        print(f"Relay listening at {relay_url}", flush=True)
        print(
            "Paste this file into your reputable Chrome: "
            f"{config.browser_script.resolve()}",
            flush=True,
        )

        if config.diagnostics is not None:
            print(
                f"Diagnostics will append to {config.diagnostics.resolve()}",
                flush=True,
            )

        print(
            "Then drive the RUC form once and click RUN CLIENTS.",
            flush=True,
        )

        try:
            while not relay_state.complete:
                server.handle_request()
        except KeyboardInterrupt:
            print("Capture relay interrupted.", file=sys.stderr)
        finally:
            server.server_close()
            store.export_current(
                config.output_csv,
                site=site.name,
                header=site.export_header,
                project=site.row,
            )

    remaining = len(rucs) - relay_state.index

    print(
        f"Summary: {relay_state.succeeded} ok, "
        f"{relay_state.rejected} rejected, "
        f"{relay_state.failed} failed, "
        f"{remaining} remaining",
        flush=True,
    )

    return (
        0
        if remaining == 0 and relay_state.rejected == 0 and relay_state.failed == 0
        else 1
    )


def main(argv: list[str] | None = None) -> None:
    raise SystemExit(run(parse_args(argv)))


if __name__ == "__main__":
    main()
