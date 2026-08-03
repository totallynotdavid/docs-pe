from __future__ import annotations

import asyncio
import logging
import time

from fetch.obs.events import CIRCUIT_CLOSED, CIRCUIT_OPEN
from fetch.obs.logging import kv


logger = logging.getLogger(__name__)

# Consecutive transient failures across a provider's lanes, with no healthy
# contact in between, before the breaker trips.
DEFAULT_THRESHOLD = 10
# Backoff doubles from base to max on each successive trip.
DEFAULT_BASE_COOLDOWN_S = 5.0
DEFAULT_MAX_COOLDOWN_S = 300.0


class CircuitBreaker:
    """Per-provider health gate shared by all its lanes.

    Lanes await acquire() before each attempt; a burst of failures parks every lane on
    that provider with exponential backoff, and the first healthy contact closes it.
    Single-threaded asyncio, so no lock.
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
        # Open means the provider, not any one document, is the suspect.
        return self._open_until > time.monotonic()

    def record_success(self) -> None:
        # A healthy contact is proof the provider is up; fully reset.
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
