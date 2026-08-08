from __future__ import annotations

import json

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
    new_webauthn_authentication_options,
    token_hash,
    totp_matches,
    verify_dummy_password,
    verify_password,
    verify_webauthn_authentication,
    webauthn_challenge_bytes,
    webauthn_challenge_text,
    webauthn_credential_id,
)


if TYPE_CHECKING:
    from portal.application.sessions import BrowserSessions, OneTimeTokens
    from portal.application.throttle import LoginThrottle
    from portal.credentials.secrets import EnvelopeProtector
    from portal.domain.models import PortalUser
    from portal.repository.audit import PostgresAuditLog
    from portal.repository.auth import PostgresAuthRepository
    from portal.turnstile import HumanCheck


# A passkey login challenge, distinct from PENDING_MFA: it names the account
# only when this is the second-factor path (see begin_passkey_login), and
# what it stores is a WebAuthn challenge rather than a bare user id.
PASSKEY_LOGIN = "passkey_login"
PASSKEY_LOGIN_TTL = timedelta(minutes=5)


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
class PasskeyLoginChallenge:
    login_token: str
    options_json: str


@dataclass(frozen=True)
class PasskeyLoginAttempt:
    login_token: str
    response_json: str
    trace: RequestTrace


@dataclass(frozen=True)
class SessionIssued:
    cookie_token: str
    needs_setup: bool = False


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
        *,
        rp_id: str,
        public_origin: str,
    ) -> None:
        self._users = users
        self._sessions = sessions
        self._tokens = tokens
        self._throttle = throttle
        self._human_check = human_check
        self._protector = protector
        self._audit = audit
        self._rp_id = rp_id
        self._public_origin = public_origin

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

        if user.has_second_factor:
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

    async def begin_passkey_login(
        self, pending_token: str | None
    ) -> PasskeyLoginChallenge:
        """pending_token names the account when called from /login/mfa (a
        passkey offered as an alternative to a TOTP code); None means a
        passwordless, discoverable login called straight from /login.

        Reads PENDING_MFA rather than consuming it: fetching options is not
        itself a guess, so it must not spend the one attempt a wrong TOTP
        code would. complete_passkey_login below is what settles PASSKEY_LOGIN.
        """
        user_id: UUID | None = None

        if pending_token is not None:
            raw = await self._tokens.peek(PENDING_MFA, pending_token)
            user_id = UUID(raw) if raw is not None else None

        allow_credential_ids: tuple[bytes, ...] = ()

        if user_id is not None:
            allow_credential_ids = tuple(
                credential.credential_id
                for credential in await self._users.webauthn_credentials(user_id)
            )

        challenge = new_webauthn_authentication_options(
            rp_id=self._rp_id,
            allow_credential_ids=allow_credential_ids,
        )

        login_token = await self._tokens.issue(
            PASSKEY_LOGIN,
            json.dumps(
                {
                    "user_id": str(user_id) if user_id is not None else None,
                    "challenge": webauthn_challenge_text(challenge.challenge),
                }
            ),
            PASSKEY_LOGIN_TTL,
        )

        return PasskeyLoginChallenge(login_token, challenge.options_json)

    async def complete_passkey_login(
        self, attempt: PasskeyLoginAttempt
    ) -> LoginOutcome:
        """A userVerification: required assertion is possession plus the
        device's own knowledge/inherence check, so unlike a TOTP code it
        already satisfies the second factor on its own: this always
        establishes with mfa_verified=True, whether reached from /login/mfa
        or directly from /login (see begin_passkey_login).
        """
        pending = await self._tokens.consume(PASSKEY_LOGIN, attempt.login_token)

        if pending is None:
            return await self._reject_passkey(attempt)

        payload = json.loads(pending)
        expected_user_id = (
            UUID(payload["user_id"]) if payload["user_id"] is not None else None
        )
        challenge = webauthn_challenge_bytes(str(payload["challenge"]))

        credential_id = webauthn_credential_id(attempt.response_json)
        stored = (
            await self._users.webauthn_credential_by_credential_id(credential_id)
            if credential_id is not None
            else None
        )

        if stored is None or (
            expected_user_id is not None and stored.user_id != expected_user_id
        ):
            return await self._reject_passkey(attempt)

        verified = verify_webauthn_authentication(
            response_json=attempt.response_json,
            expected_challenge=challenge,
            expected_origin=self._public_origin,
            expected_rp_id=self._rp_id,
            public_key=stored.public_key,
            sign_count=stored.sign_count,
        )

        if verified is None:
            return await self._reject_passkey(attempt)

        # WHERE sign_count advanced is what turns a replayed/cloned
        # assertion into a rejected write instead of a silent pass.
        if not await self._users.touch_webauthn_credential(
            stored.id,
            sign_count=verified.new_sign_count,
        ):
            return await self._reject_passkey(attempt)

        user = await self._users.user_by_id(stored.user_id)

        if user is None or not user.is_active:
            return await self._reject_passkey(attempt)

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

        return SessionIssued(cookie_token, needs_setup=user.pending_site_admin)

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

    async def _reject_passkey(self, attempt: PasskeyLoginAttempt) -> LoginRejected:
        await self._audit.record(
            AuditEvent(
                action=AuditAction.LOGIN_FAILED,
                trace=attempt.trace,
                metadata={"rejection": LoginRejection.PASSKEY_INVALID.value},
            )
        )

        return LoginRejected(LoginRejection.PASSKEY_INVALID)
