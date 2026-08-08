from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from portal.repository.auth import normalize_email


if TYPE_CHECKING:
    from uuid import UUID

    from portal.ephemeral import EphemeralStore


LOGIN_WINDOW = timedelta(minutes=5)
LOGIN_ATTEMPT_LIMIT = 5

MUTATION_WINDOW = timedelta(minutes=1)
MUTATION_LIMIT = 30


class LoginThrottle:
    """Two independent counters: one per account, one per source address.

    They are independent on purpose. Per-account alone lets one host grind
    every account in the directory; per-source alone lets a botnet grind one
    account.

    The lock is a fixed window that expires on its own, and attempts made while
    it holds are refused without being counted. That is the whole reason it can
    never become permanent: an escalating lockout would let anyone who knows an
    address keep the owner out indefinitely by attempting a login on a timer.
    The cost of the fixed window is a steady LOGIN_ATTEMPT_LIMIT guesses per
    window, which Argon2id, a 12-character minimum, and Turnstile in front of
    the form already make worthless.
    """

    def __init__(self, store: EphemeralStore) -> None:
        self._store = store

    async def allows(self, email: str, source: str) -> bool:
        for key in _login_keys(email, source):
            counted = await self._store.read(key)

            if counted is not None and int(counted) > LOGIN_ATTEMPT_LIMIT:
                return False

        return True

    async def record_failure(self, email: str, source: str) -> None:
        for key in _login_keys(email, source):
            await self._store.increment(key, LOGIN_WINDOW)

    async def clear(self, email: str, source: str) -> None:
        for key in _login_keys(email, source):
            await self._store.discard(key)


class MutationThrottle:
    """Per-actor cap on state-changing requests, counted by route family."""

    def __init__(self, store: EphemeralStore) -> None:
        self._store = store

    async def admit(self, actor_id: UUID, route_class: str) -> bool:
        """Count this request and report whether it is still within the cap."""
        used = await self._store.increment(
            f"mutate:{actor_id}:{route_class}",
            MUTATION_WINDOW,
        )

        return used <= MUTATION_LIMIT


def _login_keys(email: str, source: str) -> tuple[str, str]:
    return (
        f"login_fail:account:{normalize_email(email)}",
        f"login_fail:source:{source}",
    )
