from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from portal.application.sessions import (
    LOGIN_CSRF,
    LOGIN_CSRF_TTL,
    PENDING_MFA,
    PENDING_MFA_TTL,
)
from portal.domain.models import (
    AuditAction,
    AuditEvent,
    LoginRejection,
    RequestTrace,
)
from portal.security import (
    token_hash,
    totp_matches,
    verify_dummy_password,
    verify_password,
)


if TYPE_CHECKING:
    from portal.application.sessions import BrowserSessions, OneTimeTokens
    from portal.application.throttle import LoginThrottle
    from portal.credentials.secrets import EnvelopeProtector
    from portal.domain.models import PortalUser
    from portal.repository.audit import PostgresAuditLog
    from portal.repository.auth import PostgresAuthRepository
    from portal.turnstile import HumanCheck


@dataclass(frozen=True)
class LoginAttempt:
    email: str
    password: str
    csrf_token: str
    human_check_token: str
    trace: RequestTrace


@dataclass(frozen=True)
class MfaAttempt:
    pending_token: str
    code: str
    trace: RequestTrace


@dataclass(frozen=True)
class SessionIssued:
    cookie_token: str


@dataclass(frozen=True)
class MfaChallengeIssued:
    pending_token: str


@dataclass(frozen=True)
class LoginRejected:
    rejection: LoginRejection


LoginOutcome = SessionIssued | MfaChallengeIssued | LoginRejected


class LoginService:
    """The whole login pipeline, in the order it has to happen.

    Each step in attempt() only runs once the one before it passed, so a
    request that fails the human check never reaches the password hash, and a
    replayed form token never reaches the throttle counters.
    """

    def __init__(
        self,
        users: PostgresAuthRepository,
        sessions: BrowserSessions,
        tokens: OneTimeTokens,
        throttle: LoginThrottle,
        human_check: HumanCheck,
        protector: EnvelopeProtector,
        audit: PostgresAuditLog,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._tokens = tokens
        self._throttle = throttle
        self._human_check = human_check
        self._protector = protector
        self._audit = audit

    async def issue_login_csrf(self) -> str:
        return await self._tokens.issue(LOGIN_CSRF, "", LOGIN_CSRF_TTL)

    async def attempt(self, attempt: LoginAttempt) -> LoginOutcome:
        passed = await self._human_check.passed(
            token=attempt.human_check_token,
            client_ip=attempt.trace.source,
        )

        if not passed:
            return await self._reject(attempt, LoginRejection.HUMAN_CHECK)

        if await self._tokens.consume(LOGIN_CSRF, attempt.csrf_token) is None:
            return await self._reject(attempt, LoginRejection.CSRF)

        # The password is always verified, even when the throttle has already
        # decided the answer, so a locked account cannot be told apart from a
        # wrong password by how long the response takes.
        allowed = await self._throttle.allows(attempt.email, attempt.trace.source)
        user = await self._authenticate(attempt.email, attempt.password)

        if not allowed:
            return await self._reject(attempt, LoginRejection.THROTTLED)

        if user is None:
            await self._throttle.record_failure(attempt.email, attempt.trace.source)
            return await self._reject(attempt, LoginRejection.CREDENTIALS)

        await self._throttle.clear(attempt.email, attempt.trace.source)

        if user.mfa_enabled:
            return MfaChallengeIssued(
                await self._tokens.issue(
                    PENDING_MFA,
                    str(user.id),
                    PENDING_MFA_TTL,
                )
            )

        return await self._establish(user, attempt.trace)

    async def complete_mfa(self, attempt: MfaAttempt) -> LoginOutcome:
        pending = await self._tokens.consume(PENDING_MFA, attempt.pending_token)

        if pending is None:
            return await self._reject_mfa(None, attempt, LoginRejection.MFA_EXPIRED)

        user = await self._users.user_by_id(UUID(pending))

        if user is None:
            return await self._reject_mfa(None, attempt, LoginRejection.MFA_EXPIRED)

        if not await self._second_factor_accepted(user, attempt.code):
            await self._throttle.record_failure(user.email, attempt.trace.source)
            return await self._reject_mfa(user, attempt, LoginRejection.MFA_CODE)

        return await self._establish(user, attempt.trace, mfa_verified=True)

    async def logout(self, cookie_token: str | None, trace: RequestTrace) -> None:
        session = await self._sessions.load(cookie_token)

        await self._sessions.destroy(cookie_token)

        if session is not None:
            await self._audit.record(
                AuditEvent(
                    action=AuditAction.SESSION_DESTROYED,
                    actor_id=session.user.id,
                    trace=trace,
                )
            )

    async def _authenticate(self, email: str, password: str) -> PortalUser | None:
        found = await self._users.user_by_email(email)

        if found is None:
            verify_dummy_password(password)
            return None

        user, password_hash = found

        # Argon2id always runs, deactivated or not, so a disabled account
        # can't be told apart from a wrong password by response time.
        if not verify_password(password, password_hash):
            return None

        if not user.is_active:
            return None

        return user

    async def _second_factor_accepted(self, user: PortalUser, code: str) -> bool:
        secret = await self._users.mfa_secret(user.id)

        if secret is None:
            return False

        revealed = self._protector.reveal(secret)

        if totp_matches(revealed.decode("utf-8"), code):
            return True

        # totp_matches only accepts six digits, so anything else reaching here
        # can only be a recovery code, and spending one is a single UPDATE.
        return await self._users.consume_recovery_code(user.id, token_hash(code))

    async def verify_second_factor(
        self,
        user: PortalUser,
        code: str,
        trace: RequestTrace,
    ) -> bool:
        """Re-check an already-authenticated user's second factor, for /step-up.

        Shares the login throttle's per-account and per-source buckets with
        the login flow, so a hijacked session cannot use this endpoint to
        grind guesses beyond what a fresh login attempt could.
        """
        if not await self._throttle.allows(user.email, trace.source):
            return False

        accepted = await self._second_factor_accepted(user, code)

        if accepted:
            await self._throttle.clear(user.email, trace.source)
            await self._audit.record(
                AuditEvent(
                    action=AuditAction.STEP_UP_VERIFIED,
                    actor_id=user.id,
                    trace=trace,
                )
            )
        else:
            await self._throttle.record_failure(user.email, trace.source)
            await self._audit.record(
                AuditEvent(
                    action=AuditAction.STEP_UP_FAILED,
                    actor_id=user.id,
                    trace=trace,
                )
            )

        return accepted

    async def _establish(
        self,
        user: PortalUser,
        trace: RequestTrace,
        *,
        mfa_verified: bool = False,
    ) -> SessionIssued:
        cookie_token = await self._sessions.mint(
            user.id,
            mfa_verified_at=datetime.now(UTC) if mfa_verified else None,
        )

        await self._audit.record(
            AuditEvent(
                action=AuditAction.LOGIN_SUCCEEDED,
                actor_id=user.id,
                trace=trace,
            )
        )

        return SessionIssued(cookie_token)

    async def _reject(
        self,
        attempt: LoginAttempt,
        rejection: LoginRejection,
    ) -> LoginRejected:
        await self._audit.record(
            AuditEvent(
                action=AuditAction.LOGIN_FAILED,
                trace=attempt.trace,
                metadata={"email": attempt.email, "rejection": rejection.value},
            )
        )

        return LoginRejected(rejection)

    async def _reject_mfa(
        self,
        user: PortalUser | None,
        attempt: MfaAttempt,
        rejection: LoginRejection,
    ) -> LoginRejected:
        await self._audit.record(
            AuditEvent(
                action=AuditAction.LOGIN_FAILED,
                actor_id=user.id if user else None,
                trace=attempt.trace,
                metadata={"rejection": rejection.value},
            )
        )

        return LoginRejected(rejection)
