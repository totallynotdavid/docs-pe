from __future__ import annotations

import asyncio
import logging
import time

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fetch.obs.events import CIRCUIT_CLOSED, CIRCUIT_OPEN
from fetch.obs.logging import kv


if TYPE_CHECKING:
    from collections.abc import Callable


logger = logging.getLogger(__name__)

# Consecutive failures required to open the circuit.
DEFAULT_THRESHOLD = 10

# Cooldown doubles after each trip, up to the maximum.
DEFAULT_BASE_COOLDOWN_S = 5.0
DEFAULT_MAX_COOLDOWN_S = 300.0


@dataclass(frozen=True)
class BreakerState:
    source: str
    provider: str
    consecutive_failures: int
    level: int
    open_until: datetime | None


class CircuitBreaker:
    """Provider health gate shared by all its lanes."""

    def __init__(
        self,
        *,
        provider: str,
        run_id: str,
        source: str = "",
        threshold: int = DEFAULT_THRESHOLD,
        base_cooldown_s: float = DEFAULT_BASE_COOLDOWN_S,
        max_cooldown_s: float = DEFAULT_MAX_COOLDOWN_S,
        initial: BreakerState | None = None,
        on_change: Callable[[BreakerState], None] | None = None,
    ) -> None:
        self._provider = provider
        self._source = source
        self._run_id = run_id
        self._threshold = threshold
        self._base_cooldown_s = base_cooldown_s
        self._max_cooldown_s = max_cooldown_s
        self._consecutive = initial.consecutive_failures if initial else 0
        self._level = initial.level if initial else 0
        self._open_until = self._monotonic_deadline(
            initial.open_until if initial else None
        )
        self._open_until_at = initial.open_until if initial else None
        self._on_change = on_change

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
            self._notify()
            return

        logger.info(
            "%s %s",
            CIRCUIT_CLOSED,
            kv(run_id=self._run_id, provider=self._provider),
        )

        self._open_until = 0.0
        self._level = 0
        self._open_until_at = None
        self._notify()

    def record_failure(self) -> None:
        self._consecutive += 1

        if self._consecutive < self._threshold:
            self._notify()
            return

        self._consecutive = 0
        self._level += 1

        cooldown = min(
            self._max_cooldown_s,
            self._base_cooldown_s * (2 ** (self._level - 1)),
        )
        self._open_until = time.monotonic() + cooldown
        self._open_until_at = datetime.now(UTC) + timedelta(seconds=cooldown)

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
        self._notify()

    def _monotonic_deadline(self, open_until: datetime | None) -> float:
        if open_until is None:
            return 0.0

        return time.monotonic() + max(
            0.0,
            (open_until - datetime.now(UTC)).total_seconds(),
        )

    def _notify(self) -> None:
        if self._on_change is None:
            return

        self._on_change(
            BreakerState(
                source=self._source,
                provider=self._provider,
                consecutive_failures=self._consecutive,
                level=self._level,
                open_until=self._open_until_at,
            )
        )
