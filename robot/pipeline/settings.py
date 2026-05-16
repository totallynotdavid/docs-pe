from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerSettings:
    page_size: int
    session_budget: int
    wait_min_s: float
    wait_max_s: float
    ban_cooldown_s: float
    captcha_timeout_s: float
    captcha_timeout_first_s: float
    captcha_same_session_retries: int
    captcha_first_token_jitter_max_s: float
    chrome_binary: str = ""
    debug: bool = False
