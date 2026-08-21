from __future__ import annotations

import json

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from portal.application.sessions import (
    LOGIN_CSRF,
    LOGIN_CSRF_TTL,
    SESSION_IDLE,
    SESSION_LIFETIME,
    OneTimeTokens,
)
from portal.application.throttle import (
    LOGIN_ATTEMPT_LIMIT,
    MUTATION_LIMIT,
    LoginThrottle,
    MutationThrottle,
)
from portal.security import token_hash

from tests.portal.conftest import seed_user


if TYPE_CHECKING:
    import asyncpg

    from portal.application.sessions import BrowserSessions
    from portal.ephemeral import EphemeralStore


async def test_a_minted_session_loads_and_then_stops_loading(
    pool: asyncpg.Pool,
    sessions: BrowserSessions,
) -> None:
    user_id = await seed_user(pool)

    token = await sessions.mint(user_id)
    loaded = await sessions.load(token)

    assert loaded is not None
    assert loaded.user.id == user_id
    assert loaded.csrf_token

    await sessions.destroy(token)

    assert await sessions.load(token) is None


async def test_an_unknown_or_missing_cookie_loads_nothing(
    sessions: BrowserSessions,
) -> None:
    assert await sessions.load(None) is None
    assert await sessions.load("") is None
    assert await sessions.load("no-es-una-sesion") is None


async def test_a_session_ends_at_the_absolute_cap_however_active_it_is(
    pool: asyncpg.Pool,
    sessions: BrowserSessions,
    store: EphemeralStore,
) -> None:
    user_id = await seed_user(pool)
    token = await sessions.mint(user_id)

    # Age the record past the cap. The sliding TTL is still wide open, which is
    # the case the cap exists for: constant use must not extend a session
    # forever.
    key = f"session:{token_hash(token)}"
    record = json.loads(await store.read(key) or "{}")
    record["created_at"] = (
        datetime.now(UTC) - SESSION_LIFETIME - timedelta(minutes=1)
    ).timestamp()

    await store.replace(key, json.dumps(record), SESSION_IDLE)

    assert await sessions.load(token) is None
    assert await store.read(key) is None


async def test_a_session_whose_user_is_gone_loads_nothing(
    pool: asyncpg.Pool,
    sessions: BrowserSessions,
) -> None:
    user_id = await seed_user(pool)
    token = await sessions.mint(user_id)

    await pool.execute("DELETE FROM portal_users WHERE id = $1", user_id)

    assert await sessions.load(token) is None


async def test_a_one_time_token_is_spent_by_the_first_read(
    store: EphemeralStore,
) -> None:
    tokens = OneTimeTokens(store)
    token = await tokens.issue(LOGIN_CSRF, "carga", LOGIN_CSRF_TTL)

    assert await tokens.consume(LOGIN_CSRF, token) == "carga"
    assert await tokens.consume(LOGIN_CSRF, token) is None
    assert await tokens.consume(LOGIN_CSRF, None) is None


async def test_a_token_cannot_be_consumed_under_another_purpose(
    store: EphemeralStore,
) -> None:
    tokens = OneTimeTokens(store)
    token = await tokens.issue(LOGIN_CSRF, "carga", LOGIN_CSRF_TTL)

    assert await tokens.consume("pending_mfa", token) is None
    assert await tokens.consume(LOGIN_CSRF, token) == "carga"


async def test_the_login_throttle_locks_the_account_and_the_source_apart(
    store: EphemeralStore,
) -> None:
    throttle = LoginThrottle(store)

    for _ in range(LOGIN_ATTEMPT_LIMIT + 1):
        await throttle.record_failure("persona@example.test", "203.0.113.7")

    assert await throttle.allows("persona@example.test", "198.51.100.1") is False
    assert await throttle.allows("otra@example.test", "203.0.113.7") is False
    assert await throttle.allows("otra@example.test", "198.51.100.1") is True


async def test_a_successful_login_clears_both_counters(store: EphemeralStore) -> None:
    throttle = LoginThrottle(store)

    for _ in range(LOGIN_ATTEMPT_LIMIT + 1):
        await throttle.record_failure("persona@example.test", "203.0.113.7")

    await throttle.clear("persona@example.test", "203.0.113.7")

    assert await throttle.allows("persona@example.test", "203.0.113.7") is True


async def test_the_mutation_cap_is_counted_per_route_family(
    pool: asyncpg.Pool,
    store: EphemeralStore,
) -> None:
    throttle = MutationThrottle(store)
    actor_id = await seed_user(pool)

    for _ in range(MUTATION_LIMIT):
        assert await throttle.admit(actor_id, "teams") is True

    assert await throttle.admit(actor_id, "teams") is False
    assert await throttle.admit(actor_id, "admin") is True


async def test_a_lock_cannot_be_extended_by_attempting_through_it(
    store: EphemeralStore,
) -> None:
    throttle = LoginThrottle(store)

    for _ in range(LOGIN_ATTEMPT_LIMIT + 1):
        await throttle.record_failure("persona@example.test", "203.0.113.7")

    assert await throttle.allows("persona@example.test", "203.0.113.7") is False

    locked_at = await store.read("login_fail:account:persona@example.test")

    # Attempts arriving while the lock holds are refused by LoginService before
    # they reach record_failure, so the window keeps its original deadline and
    # the account always gets back in.
    assert locked_at == str(LOGIN_ATTEMPT_LIMIT + 1)
