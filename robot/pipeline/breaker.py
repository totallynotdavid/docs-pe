from __future__ import annotations

import asyncio
import logging
import time

from robot.obs.events import CIRCUIT_CLOSED, CIRCUIT_OPEN
from robot.obs.logging import kv


logger = logging.getLogger(__name__)

# Consecutive transient failures across a provider's lanes, with no healthy
# contact in between, before the breaker trips. High enough that sporadic ban or
# transport noise never trips it; a sustained outage (proxy credits gone, gateway
# down) crosses it in seconds.
DEFAULT_THRESHOLD = 10
# Backoff doubles from base to max on each successive trip, so a long outage parks
# the lanes instead of grinding the backlog into cumulative attempts.
DEFAULT_BASE_COOLDOWN_S = 5.0
DEFAULT_MAX_COOLDOWN_S = 300.0


class CircuitBreaker:
    """Per-provider health gate shared by all its lanes.

    Lanes await acquire() before each attempt; a burst of transient failures parks
    every lane on that provider with exponential backoff, and the first healthy
    contact closes it. No lock is needed: asyncio's single thread makes the counter
    updates atomic with respect to each other.
    """

    def __init__(
        self,
        *,
        provider: str,
        run_id: str,
        threshold: int = DEFAULT_THRESHOLD,
        base_cooldown_s: float = DEFAULT_BASE_COOLDOWN_S,
        max_cooldown_s: float = DEFAULT_MAX_COOLDOWN_S,
    ) -> None:
        self._provider = provider
        self._run_id = run_id
        self._threshold = threshold
        self._base = base_cooldown_s
        self._max = max_cooldown_s
        self._consecutive = 0
        self._level = 0
        self._open_until = 0.0

    async def acquire(self) -> None:
        while True:
            remaining = self._open_until - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    def is_open(self) -> bool:
        # Open means the provider is in a tripped cooldown: the environment, not any
        # one RUC, is the suspect.
        return self._open_until > time.monotonic()

    def record_success(self) -> None:
        # A completed lookup is proof the environment is up, so fully reset.
        self._consecutive = 0
        if self._open_until:
            logger.info(
                "%s %s",
                CIRCUIT_CLOSED,
                kv(run_id=self._run_id, provider=self._provider),
            )
            self._open_until = 0.0
            self._level = 0

    def record_failure(self) -> None:
        self._consecutive += 1
        if self._consecutive < self._threshold:
            return
        self._consecutive = 0
        self._level += 1
        cooldown = min(self._max, self._base * (2 ** (self._level - 1)))
        self._open_until = time.monotonic() + cooldown
        logger.warning(
            "%s %s",
            CIRCUIT_OPEN,
            kv(
                run_id=self._run_id,
                provider=self._provider,
                cooldown_s=round(cooldown, 1),
                level=self._level,
            ),
        )
