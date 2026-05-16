from __future__ import annotations

import argparse

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunConfig:
    input_csv: Path
    output_csv: Path
    page_size: int
    workers: int
    dedupe: bool
    debug: bool
    session_budget: int
    wait_min_s: float
    wait_max_s: float
    ban_cooldown_s: float
    captcha_timeout_s: float
    captcha_timeout_first_s: float
    captcha_same_session_retries: int
    captcha_first_token_jitter_max_s: float
    env_file: str


def load_config(argv: list[str] | None = None) -> RunConfig:
    parser = argparse.ArgumentParser(prog="robot")
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--dedupe", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--debug", action="store_true", default=False)
    parser.add_argument("--session-budget", type=int, default=5)
    parser.add_argument("--wait-min-s", type=float, default=10.0)
    parser.add_argument("--wait-max-s", type=float, default=15.0)
    parser.add_argument("--ban-cooldown-s", type=float, default=180.0)
    parser.add_argument("--captcha-timeout-s", type=float, default=20.0)
    parser.add_argument("--captcha-timeout-first-s", type=float, default=40.0)
    parser.add_argument("--captcha-same-session-retries", type=int, default=1)
    parser.add_argument("--captcha-first-token-jitter-max-s", type=float, default=5.0)
    parser.add_argument("--env-file", default=".env")
    ns = parser.parse_args(argv)

    errors: list[str] = []
    if ns.page_size < 1:
        errors.append("--page-size must be >= 1")
    if ns.workers < 1:
        errors.append("--workers must be >= 1")
    if ns.session_budget < 1:
        errors.append("--session-budget must be >= 1")
    if ns.wait_min_s < 0:
        errors.append("--wait-min-s must be >= 0")
    if ns.wait_max_s < ns.wait_min_s:
        errors.append("--wait-max-s must be >= --wait-min-s")
    if ns.ban_cooldown_s < 0:
        errors.append("--ban-cooldown-s must be >= 0")
    if ns.captcha_timeout_s <= 0:
        errors.append("--captcha-timeout-s must be > 0")
    if ns.captcha_timeout_first_s <= 0:
        errors.append("--captcha-timeout-first-s must be > 0")
    if ns.captcha_timeout_first_s < ns.captcha_timeout_s:
        errors.append("--captcha-timeout-first-s must be >= --captcha-timeout-s")
    if ns.captcha_same_session_retries < 0:
        errors.append("--captcha-same-session-retries must be >= 0")
    if ns.captcha_first_token_jitter_max_s < 0:
        errors.append("--captcha-first-token-jitter-max-s must be >= 0")
    if errors:
        parser.error("; ".join(errors))

    return RunConfig(
        input_csv=ns.input,
        output_csv=ns.output,
        page_size=ns.page_size,
        workers=ns.workers,
        dedupe=ns.dedupe,
        debug=ns.debug,
        session_budget=ns.session_budget,
        wait_min_s=ns.wait_min_s,
        wait_max_s=ns.wait_max_s,
        ban_cooldown_s=ns.ban_cooldown_s,
        captcha_timeout_s=ns.captcha_timeout_s,
        captcha_timeout_first_s=ns.captcha_timeout_first_s,
        captcha_same_session_retries=ns.captcha_same_session_retries,
        captcha_first_token_jitter_max_s=ns.captcha_first_token_jitter_max_s,
        env_file=ns.env_file,
    )
