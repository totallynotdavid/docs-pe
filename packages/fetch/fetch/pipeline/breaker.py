from __future__ import annotations

import asyncio
import logging
import time

from fetch.obs.events import CIRCUIT_CLOSED, CIRCUIT_OPEN
from fetch.obs.logging import kv


logger = logging.getLogger(__name__)

# Consecutive failures required to open the circuit.
DEFAULT_THRESHOLD = 10

# Cooldown doubles after each trip, up to the maximum.
DEFAULT_BASE_COOLDOWN_S = 5.0
DEFAULT_MAX_COOLDOWN_S = 300.0


class CircuitBreaker:
    """Provider health gate shared by all its lanes."""

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
        self._base_cooldown_s = base_cooldown_s
        self._max_cooldown_s = max_cooldown_s
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
        return self._open_until > time.monotonic()

    def record_success(self) -> None:
        self._consecutive = 0

        if not self._open_until:
            return

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

        cooldown = min(
            self._max_cooldown_s,
            self._base_cooldown_s * (2 ** (self._level - 1)),
        )
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
