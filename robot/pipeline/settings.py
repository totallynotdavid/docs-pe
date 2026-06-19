from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class WorkerSettings:
    page_size: int
    session_budget: int
    wait_min_s: float
    wait_max_s: float
    ban_cooldown_s: float
    debug: bool = False
