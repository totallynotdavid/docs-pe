from __future__ import annotations

import json
import secrets

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from portal.domain.errors import PermissionDenied, Reason
from portal.domain.models import BrowserSession
from portal.security import new_csrf_token, new_session_token, token_hash, valid_csrf


if TYPE_CHECKING:
    from portal.ephemeral import EphemeralStore
    from portal.repository.auth import PostgresAuthRepository


# Idle expiry is the store's TTL, refreshed on every authenticated request. The
# absolute cap is enforced here because a sliding TTL cannot express "no matter
# how active, this session ends": without it a stolen cookie lives forever.
SESSION_IDLE = timedelta(hours=12)
SESSION_LIFETIME = timedelta(days=7)

LOGIN_CSRF_TTL = timedelta(minutes=10)
PENDING_MFA_TTL = timedelta(minutes=5)

LOGIN_CSRF = "login_csrf"
PENDING_MFA = "pending_mfa"


class BrowserSessions:
    """Cookie sessions in the ephemeral store, keyed by the hashed cookie.

    The cookie value itself is never stored, so a store dump does not yield
    usable cookies, and the identity behind a session is re-read from Postgres
    on every request rather than cached in the record: revoking an
    administrator takes effect on their next request, not at expiry.
    """

    def __init__(self, store: EphemeralStore, users: PostgresAuthRepository) -> None:
        self._store = store
        self._users = users

    async def mint(
        self,
        user_id: UUID,
        *,
        mfa_verified_at: datetime | None = None,
    ) -> str:
        token = new_session_token()

        await self._store.put_new(
            _session_key(token),
            json.dumps(
                {
                    "user_id": str(user_id),
                    "csrf_token": new_csrf_token(),
                    "created_at": datetime.now(UTC).timestamp(),
                    "mfa_verified_at": (
                        mfa_verified_at.timestamp() if mfa_verified_at else None
                    ),
                }
            ),
            SESSION_IDLE,
        )

        return token

    async def load(self, token: str | None) -> BrowserSession | None:
        if not token:
            return None

        key = _session_key(token)
        stored = await self._store.read(key)

        if stored is None:
            return None

        record = json.loads(stored)
        age = datetime.now(UTC).timestamp() - float(record["created_at"])

        if age > SESSION_LIFETIME.total_seconds():
            await self._store.discard(key)
            return None

        # Conditional refresh: a session destroyed between the read and here
        # must stay destroyed rather than be written back.
        if not await self._store.replace(key, stored, SESSION_IDLE):
            return None

        user = await self._users.user_by_id(UUID(record["user_id"]))

        # Re-read on every request, same as the identity check above: a
        # deactivation takes effect on the account's next request, not at
        # the session's natural idle/absolute expiry.
        if user is None or not user.is_active:
            await self._store.discard(key)
            return None

        return BrowserSession(user, str(record["csrf_token"]), _verified_at(record))

    async def verify_csrf(
        self,
        token: str | None,
        submitted: str | None,
    ) -> BrowserSession:
        session = await self.load(token)

        if session is None or not valid_csrf(submitted, session.csrf_token):
            raise PermissionDenied(Reason.CSRF_INVALID)

        return session

    async def destroy(self, token: str | None) -> None:
        if token:
            await self._store.discard(_session_key(token))

    async def mark_step_up_verified(self, token: str | None) -> bool:
        """Stamp fresh second-factor proof onto an already-open session.

        Distinct from mint()'s mfa_verified_at: a long-lived session earns
        this by completing MFA a second time mid-session (see /step-up), not
        just by having done so once at login.
        """
        if not token:
            return False

        key = _session_key(token)
        stored = await self._store.read(key)

        if stored is None:
            return False

        record = json.loads(stored)
        record["mfa_verified_at"] = datetime.now(UTC).timestamp()

        return await self._store.replace(key, json.dumps(record), SESSION_IDLE)


class OneTimeTokens:
    """Short-lived tokens that are spent on first use.

    Single use is a property of the read, not of a cleanup pass: taking a token
    removes it in the same operation, so a replay finds nothing.
    """

    def __init__(self, store: EphemeralStore) -> None:
        self._store = store

    async def issue(self, purpose: str, payload: str, ttl: timedelta) -> str:
        token = secrets.token_urlsafe(32)

        await self._store.put_new(_token_key(purpose, token), payload, ttl)

        return token

    async def consume(self, purpose: str, token: str | None) -> str | None:
        if not token:
            return None

        return await self._store.take(_token_key(purpose, token))


def _verified_at(record: dict[str, Any]) -> datetime | None:
    stamp = record.get("mfa_verified_at")

    return None if stamp is None else datetime.fromtimestamp(float(stamp), tz=UTC)


def _session_key(token: str) -> str:
    return f"session:{token_hash(token)}"


def _token_key(purpose: str, token: str) -> str:
    return f"{purpose}:{token_hash(token)}"
