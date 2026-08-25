from __future__ import annotations

import json
import re

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

from fetch.domain.errors import ProxyConfigurationError
from fetch.proxy.registry import PROVIDERS, preflight, spec_for

from portal.application.access import (
    AuthorizedService,
    public,
    site_admin,
    site_admin_or_leader,
    site_admin_step_up,
)
from portal.branding import PRODUCT_NAME
from portal.credentials.secrets import EnvelopeProtector, encode_config
from portal.domain.errors import (
    CredentialConfigurationError,
    NotFound,
    PermissionDenied,
    ProvisioningError,
    Reason,
)
from portal.domain.models import (
    AuditAction,
    AuditEvent,
    CredentialState,
    CredentialVersion,
    PortalUser,
    RequestTrace,
    Team,
    TeamInvite,
    TeamRole,
    WebAuthnCredential,
)
from portal.security import (
    hash_password,
    new_recovery_codes,
    new_session_token,
    new_totp_secret,
    new_webauthn_registration_options,
    token_hash,
    totp_enrollment_uri,
    totp_matches,
    verify_webauthn_registration,
    webauthn_challenge_bytes,
    webauthn_challenge_text,
)


if TYPE_CHECKING:
    from fetch.proxy.base import Field

    from portal.application.sessions import OneTimeTokens
    from portal.notify.mailer import Mailer
    from portal.repository.audit import PostgresAuditLog
    from portal.repository.auth import PostgresAuthRepository
    from portal.repository.credentials import PostgresCredentialRepository
    from portal.repository.teams import PostgresTeamRepository

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_SLUGIFY = re.compile(r"[^a-z0-9]+")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIN_PASSWORD = 12

# Setup tokens confirm enrollment. They are never valid login credentials.
_TOTP_SETUP = "totp_setup"
_TOTP_SETUP_TTL = timedelta(minutes=10)
_PASSKEY_SETUP = "passkey_setup"
_PASSKEY_SETUP_TTL = timedelta(minutes=5)

_INVITE_TTL = timedelta(days=7)


@dataclass(frozen=True)
class InstallationStatus:
    team_count: int
    initial_team_id: UUID | None
    can_create_first_team: bool
    next_step: str


@dataclass(frozen=True)
class TeamReadiness:
    has_active_credential: bool
    has_members_beyond_creator: bool
    next_step: str


@dataclass(frozen=True)
class FirstTeamResult:
    team: Team
    created: bool


@dataclass(frozen=True)
class TotpSetup:
    """Not yet enabled: confirm_totp_setup makes it live."""

    setup_token: str
    enrollment_uri: str


@dataclass(frozen=True)
class PasskeySetup:
    """Not yet enabled: confirm_passkey_registration makes it live."""

    setup_token: str
    options_json: str


class ProvisioningService(AuthorizedService):
    def __init__(
        self,
        auth: PostgresAuthRepository,
        teams: PostgresTeamRepository,
        credentials: PostgresCredentialRepository,
        protector: EnvelopeProtector,
        audit: PostgresAuditLog,
        issuer: str,
        *,
        public_origin: str,
        setup_tokens: OneTimeTokens,
        mailer: Mailer,
    ) -> None:
        self._auth = auth
        self._teams = teams
        self._credentials = credentials
        self._protector = protector
        self._audit = audit
        # WebAuthn requires the RP ID and origin to use the request hostname.
        self._issuer = issuer
        self._public_origin = public_origin
        self._setup_tokens = setup_tokens
        self._mailer = mailer

    @public
    async def require_site_admin(self, actor_id: UUID) -> None:
        await self._require_site_admin(actor_id)

    @site_admin
    async def installation_status(self, actor_id: UUID) -> InstallationStatus:
        team_count, initial_team_id = await self._teams.installation_status()
        can_create_first_team = team_count == 0 and initial_team_id is None

        return InstallationStatus(
            team_count=team_count,
            initial_team_id=initial_team_id,
            can_create_first_team=can_create_first_team,
            next_step=("create_first_team" if can_create_first_team else "manage_site"),
        )

    @public
    async def ensure_site_admin(
        self,
        email: str,
        password_hash: str,
    ) -> tuple[PortalUser, bool]:
        """Create or verify the initial administrator.

        Returns (user, needs_setup). The account completes enrollment at
        /security/setup.
        """
        user = await self._auth.create_account(email, password_hash)

        if user.has_second_factor:
            return user, False

        await self._auth.set_pending_site_admin(user.id, pending=True)

        return await self._reload(user.id), True

    @site_admin
    async def create_first_team(
        self,
        actor_id: UUID,
        *,
        name: str,
        slug: str | None = None,
        trace: RequestTrace,
    ) -> Team:
        clean_name = self._name(name)
        team = await self._teams.create_first_team(
            self._slug(slug) if slug else await self._unique_slug(clean_name),
            clean_name,
            actor_id,
        )

        await self._record(AuditAction.TEAM_CREATED, actor_id, trace, team=team.id)

        return team

    @site_admin
    async def ensure_first_team(
        self,
        actor_id: UUID,
        *,
        name: str,
        slug: str,
    ) -> FirstTeamResult:
        """Rerunning bootstrap against an existing installation verifies it."""
        normalized_slug = self._slug(slug)
        team_count, initial_team_id = await self._teams.installation_status()

        if team_count == 0:
            team = await self.create_first_team(
                actor_id,
                name=name,
                slug=normalized_slug,
                trace=RequestTrace(),
            )
            return FirstTeamResult(team, created=True)

        existing = await self._teams.team_by_slug(normalized_slug)

        if existing is None or existing.id != initial_team_id:
            raise ProvisioningError(Reason.INITIAL_TEAM_MISMATCH)

        await self._teams.add_member(
            existing.id,
            actor_id,
            TeamRole.TEAM_LEADER,
        )

        return FirstTeamResult(existing, created=False)

    @site_admin_step_up()
    async def create_team(
        self,
        actor_id: UUID,
        *,
        name: str,
        slug: str | None = None,
        leader_email: str,
        mfa_verified_at: datetime | None,
        trace: RequestTrace,
    ) -> Team:
        leader = await self._user_by_email(leader_email)
        clean_name = self._name(name)

        team = await self._teams.create_team(
            self._slug(slug) if slug else await self._unique_slug(clean_name),
            clean_name,
            actor_id,
            leader.id,
        )

        await self._record(AuditAction.TEAM_CREATED, actor_id, trace, team=team.id)

        return team

    @site_admin_step_up()
    async def create_user(
        self,
        actor_id: UUID,
        *,
        email: str,
        password: str,
        mfa_verified_at: datetime | None,
        trace: RequestTrace,
    ) -> PortalUser:
        if len(password) < _MIN_PASSWORD:
            raise ProvisioningError(
                Reason.PASSWORD_TOO_SHORT,
                minimum=_MIN_PASSWORD,
            )

        user = await self._auth.create_user(
            self._email(email),
            hash_password(password),
        )

        await self._record(AuditAction.USER_CREATED, actor_id, trace, user=user.id)

        return user

    @site_admin
    async def users(self, actor_id: UUID) -> tuple[PortalUser, ...]:
        return await self._teams.users()

    @site_admin
    async def user_detail(self, actor_id: UUID, user_id: UUID) -> PortalUser:
        user = await self._auth.user_by_id(user_id)

        if user is None:
            raise NotFound(Reason.USER_NOT_FOUND)

        return user

    @site_admin
    async def teams_for_user(
        self,
        actor_id: UUID,
        user_id: UUID,
    ) -> tuple[Team, ...]:
        return await self._teams.teams_for_user_detail(user_id)

    @site_admin
    async def is_sole_active_admin(self, actor_id: UUID, user_id: UUID) -> bool:
        """UI hint only: whether removing this person's admin standing would
        need to be blocked. Not itself a guard; see _guard_account_removal
        and portal_installation_must_have_admin for the real enforcement.
        """
        user = await self._auth.user_by_id(user_id)

        if user is None or not user.is_site_admin or not user.is_active:
            return False

        return await self._auth.count_active_site_admins() <= 1

    @site_admin_step_up()
    async def deactivate_user(
        self,
        actor_id: UUID,
        *,
        user_id: UUID,
        mfa_verified_at: datetime | None,
        trace: RequestTrace,
    ) -> PortalUser:
        await self._guard_account_removal(actor_id, user_id)

        user = await self._auth.deactivate(user_id, actor_id)

        await self._record(AuditAction.USER_DEACTIVATED, actor_id, trace, user=user_id)

        return user

    @site_admin_step_up()
    async def reactivate_user(
        self,
        actor_id: UUID,
        *,
        user_id: UUID,
        mfa_verified_at: datetime | None,
        trace: RequestTrace,
    ) -> PortalUser:
        user = await self._auth.reactivate(user_id)

        await self._record(AuditAction.USER_REACTIVATED, actor_id, trace, user=user_id)

        return user

    @site_admin_step_up()
    async def delete_user(
        self,
        actor_id: UUID,
        *,
        user_id: UUID,
        mfa_verified_at: datetime | None,
        trace: RequestTrace,
    ) -> None:
        await self._guard_account_removal(actor_id, user_id)

        if not await self._auth.delete_if_unused(user_id):
            raise ProvisioningError(Reason.USER_HAS_HISTORY)

        await self._record(AuditAction.USER_DELETED, actor_id, trace, user=user_id)

    @site_admin_step_up()
    async def promote_to_site_admin(
        self,
        actor_id: UUID,
        *,
        user_id: UUID,
        mfa_verified_at: datetime | None,
        trace: RequestTrace,
    ) -> bool:
        """Promote, or mark the target pending their own enrollment.

        Returns needs_setup. A target who already self-enrolled a factor (see
        the self-service methods below) promotes immediately: the promoting
        admin never generates or sees another account's second factor, only
        whether one is still needed.
        """
        user = await self._auth.user_by_id(user_id)

        if user is None:
            raise NotFound(Reason.USER_NOT_FOUND)

        if user.is_site_admin:
            return False

        if user.has_second_factor:
            await self._auth.promote_now(user_id)
            needs_setup = False
        else:
            await self._auth.set_pending_site_admin(user_id, pending=True)
            needs_setup = True

        await self._record(AuditAction.USER_PROMOTED, actor_id, trace, user=user_id)

        return needs_setup

    @site_admin_step_up()
    async def demote_site_admin(
        self,
        actor_id: UUID,
        *,
        user_id: UUID,
        mfa_verified_at: datetime | None,
        trace: RequestTrace,
    ) -> PortalUser:
        if user_id == actor_id:
            raise ProvisioningError(Reason.USER_CANNOT_DEACTIVATE_SELF)

        # No app-level "last admin" pre-check here: the actor must itself be
        # an active site admin (site_admin_step_up) and can't target itself
        # (just above), so a different, active admin target always leaves at
        # least the actor standing. portal_installation_must_have_admin is
        # the real backstop, for the concurrent-request case this can't
        # reason about.
        user = await self._auth.demote(user_id)

        await self._record(AuditAction.USER_DEMOTED, actor_id, trace, user=user_id)

        return user

    @site_admin_step_up()
    async def reset_password(
        self,
        actor_id: UUID,
        *,
        user_id: UUID,
        password: str,
        mfa_verified_at: datetime | None,
        trace: RequestTrace,
    ) -> None:
        if len(password) < _MIN_PASSWORD:
            raise ProvisioningError(Reason.PASSWORD_TOO_SHORT, minimum=_MIN_PASSWORD)

        await self._auth.set_password(user_id, hash_password(password))

        await self._record(
            AuditAction.USER_PASSWORD_RESET,
            actor_id,
            trace,
            user=user_id,
        )

    # --- Self-service second factors -----------------------------------
    #
    # @public here means what it always means in this file: no role check,
    # because every one of these acts on the caller's own account
    # (actor_id is always "myself"). Open to any signed-in user, not just
    # site admins: portal_admin_requires_second_factor only requires one of
    # a site admin, but nothing stops a team member from adding their own.

    @public
    async def begin_totp_setup(self, actor_id: UUID) -> TotpSetup:
        user = await self._reload(actor_id)
        secret = new_totp_secret()

        setup_token = await self._setup_tokens.issue(
            _TOTP_SETUP,
            json.dumps({"user_id": str(user.id), "secret": secret}),
            _TOTP_SETUP_TTL,
        )

        return TotpSetup(
            setup_token=setup_token,
            enrollment_uri=totp_enrollment_uri(
                secret,
                email=user.email,
                issuer=self._issuer,
            ),
        )

    @public
    async def confirm_totp_setup(
        self,
        actor_id: UUID,
        *,
        setup_token: str,
        code: str,
    ) -> tuple[str, ...] | None:
        """Confirm the setup token without spending it on an invalid code.

        A wrong code does not spend setup_token: the secret is a long-lived QR
        the user already has in
        front of them, not a guessable target, so a typo should mean "try
        again" rather than "scan a new code." Only a successful confirm (or
        the token's own TTL) retires it.
        """
        user = await self._reload(actor_id)
        pending = await self._peek_setup(_TOTP_SETUP, setup_token, user.id)
        secret = str(pending["secret"])

        if not totp_matches(secret, code):
            raise ProvisioningError(Reason.TOTP_CODE_INVALID)

        await self._setup_tokens.consume(_TOTP_SETUP, setup_token)

        return await self._commit_totp(user, secret)

    @public
    async def disable_totp(self, actor_id: UUID) -> None:
        await self._auth.disable_totp(actor_id)
        await self._record(AuditAction.MFA_REMOVED, actor_id, RequestTrace())

    @public
    async def begin_passkey_registration(self, actor_id: UUID) -> PasskeySetup:
        user = await self._reload(actor_id)
        existing = await self._auth.webauthn_credentials(user.id)

        challenge = new_webauthn_registration_options(
            rp_id=self._issuer,
            rp_name=self._issuer,
            user_id=user.id.bytes,
            user_email=user.email,
            exclude_credential_ids=[
                credential.credential_id for credential in existing
            ],
        )

        setup_token = await self._setup_tokens.issue(
            _PASSKEY_SETUP,
            json.dumps(
                {
                    "user_id": str(user.id),
                    "challenge": webauthn_challenge_text(challenge.challenge),
                }
            ),
            _PASSKEY_SETUP_TTL,
        )

        return PasskeySetup(
            setup_token=setup_token, options_json=challenge.options_json
        )

    @public
    async def confirm_passkey_registration(
        self,
        actor_id: UUID,
        *,
        setup_token: str,
        response_json: str,
        label: str,
    ) -> tuple[str, ...] | None:
        user = await self._reload(actor_id)
        pending = await self._consume_setup(_PASSKEY_SETUP, setup_token, user.id)

        verified = verify_webauthn_registration(
            response_json=response_json,
            expected_challenge=webauthn_challenge_bytes(str(pending["challenge"])),
            expected_origin=self._public_origin,
            expected_rp_id=self._issuer,
        )

        if verified is None:
            raise ProvisioningError(Reason.WEBAUTHN_VERIFICATION_FAILED)

        first_factor = not user.has_second_factor
        recovery_codes = new_recovery_codes() if first_factor else None

        await self._auth.add_webauthn_credential(
            user.id,
            credential_id=verified.credential_id,
            public_key=verified.public_key,
            sign_count=verified.sign_count,
            transports=verified.transports,
            label=self._label(label) if label.strip() else "Clave de acceso",
            recovery_code_hashes=(
                None
                if recovery_codes is None
                else tuple(token_hash(code) for code in recovery_codes)
            ),
            promote_to_site_admin=user.pending_site_admin,
        )

        await self._record(AuditAction.PASSKEY_REGISTERED, user.id, RequestTrace())

        return recovery_codes

    @public
    async def passkeys(self, actor_id: UUID) -> tuple[WebAuthnCredential, ...]:
        return await self._auth.webauthn_credentials(actor_id)

    @public
    async def remove_passkey(self, actor_id: UUID, *, credential_id: UUID) -> None:
        await self._auth.remove_webauthn_credential(actor_id, credential_id)
        await self._record(AuditAction.PASSKEY_REMOVED, actor_id, RequestTrace())

    @site_admin
    async def teams(self, actor_id: UUID) -> tuple[Team, ...]:
        return await self._teams.all_teams()

    @site_admin_or_leader()
    async def invite_or_add_member(
        self,
        actor_id: UUID,
        *,
        team_id: UUID,
        email: str,
        role: TeamRole,
        trace: RequestTrace,
    ) -> None:
        """Add directly when the email already has an account; otherwise
        invite it. A team leader can only add someone who already has a
        platform account through /admin/users, which they cannot reach:
        inviting by email is the only door available to them.
        """
        if role not in {TeamRole.TEAM_LEADER, TeamRole.TEAM_MEMBER}:
            raise ProvisioningError(Reason.ROLE_INVALID)

        clean_email = self._email(email)
        found = await self._auth.user_by_email(clean_email)

        if found is not None:
            member = found[0]
            await self._teams.add_member(team_id, member.id, role)

            await self._record(
                AuditAction.MEMBER_ADDED,
                actor_id,
                trace,
                team=team_id,
                user=member.id,
                role=role.value,
            )
            return

        await self._send_invite(
            actor_id,
            team_id=team_id,
            email=clean_email,
            role=role,
            trace=trace,
        )

    async def _send_invite(
        self,
        actor_id: UUID,
        *,
        team_id: UUID,
        email: str,
        role: TeamRole,
        trace: RequestTrace,
    ) -> None:
        token = new_session_token()

        await self._teams.create_invite(
            team_id,
            email,
            role,
            token_hash=token_hash(token),
            invited_by=actor_id,
            expires_at=datetime.now(UTC) + _INVITE_TTL,
        )

        await self._mailer.send(
            to=email,
            subject=f"Te invitaron a un equipo en {PRODUCT_NAME}",
            body=(
                f"Te invitaron a un equipo en {PRODUCT_NAME}. Para unirte, abre este "
                f"enlace y crea tu contraseña: {self._public_origin}/invite/{token}"
            ),
        )

        await self._record(
            AuditAction.INVITE_SENT,
            actor_id,
            trace,
            team=team_id,
            email=email,
            role=role.value,
        )

    @site_admin_or_leader()
    async def resend_invite(
        self,
        actor_id: UUID,
        *,
        team_id: UUID,
        invite_id: UUID,
        trace: RequestTrace,
    ) -> None:
        invites = await self._teams.pending_invites_for_team(team_id)
        invite = next((each for each in invites if each.id == invite_id), None)

        if invite is None:
            raise NotFound(Reason.INVITE_INVALID)

        await self._send_invite(
            actor_id,
            team_id=team_id,
            email=invite.email,
            role=invite.role,
            trace=trace,
        )

    @site_admin_or_leader()
    async def cancel_invite(
        self,
        actor_id: UUID,
        *,
        team_id: UUID,
        invite_id: UUID,
    ) -> None:
        await self._teams.delete_invite(team_id, invite_id)

    @site_admin_or_leader()
    async def pending_invites(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> tuple[TeamInvite, ...]:
        return await self._teams.pending_invites_for_team(team_id)

    @public
    async def invite_preview(self, *, token: str) -> TeamInvite:
        return await self._valid_invite(token)

    @public
    async def redeem_invite(self, *, token: str, password: str) -> PortalUser:
        invite = await self._valid_invite(token)

        if len(password) < _MIN_PASSWORD:
            raise ProvisioningError(Reason.PASSWORD_TOO_SHORT, minimum=_MIN_PASSWORD)

        found = await self._auth.user_by_email(invite.email)
        user = (
            found[0]
            if found is not None
            else await self._auth.create_user(invite.email, hash_password(password))
        )

        await self._teams.add_member(invite.team_id, user.id, invite.role)
        await self._teams.mark_invite_accepted(invite.id)

        await self._record(
            AuditAction.INVITE_ACCEPTED,
            user.id,
            RequestTrace(),
            team=invite.team_id,
        )

        return user

    async def _valid_invite(self, token: str) -> TeamInvite:
        invite = await self._teams.invite_by_token_hash(token_hash(token))

        if (
            invite is None
            or not invite.is_pending
            or invite.expires_at <= datetime.now(UTC)
        ):
            raise NotFound(Reason.INVITE_INVALID)

        return invite

    @site_admin_or_leader()
    async def remove_member(
        self,
        actor_id: UUID,
        *,
        team_id: UUID,
        email: str,
        trace: RequestTrace,
    ) -> None:
        member = await self._user_by_email(email)

        await self._teams.remove_member(team_id, member.id)

        await self._record(
            AuditAction.MEMBER_REMOVED,
            actor_id,
            trace,
            team=team_id,
            user=member.id,
        )

    @site_admin_or_leader()
    async def members(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> tuple[tuple[PortalUser, TeamRole], ...]:
        return await self._teams.members_for_team(team_id)

    @site_admin_or_leader()
    async def team_readiness(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> TeamReadiness:
        credentials = await self._credentials.credentials_for_team(team_id)
        has_active_credential = any(
            credential.is_active and credential.state is CredentialState.ACTIVE
            for credential in credentials
        )
        members = await self._teams.members_for_team(team_id)

        return TeamReadiness(
            has_active_credential=has_active_credential,
            has_members_beyond_creator=len(members) > 1,
            next_step=("submit_job" if has_active_credential else "configure_proxy"),
        )

    @site_admin_or_leader()
    async def configure_proxy(
        self,
        actor_id: UUID,
        *,
        team_id: UUID,
        label: str,
        provider: str,
        values: Mapping[str, str],
        trace: RequestTrace,
    ) -> CredentialVersion:
        if provider not in PROVIDERS:
            raise CredentialConfigurationError(Reason.PROXY_UNAVAILABLE)

        clean_label = self._label(label)
        spec = spec_for(provider)

        try:
            normalized = spec.normalize(values)
        except ProxyConfigurationError as error:
            raise CredentialConfigurationError(
                Reason.PROXY_INVALID,
            ) from error

        protected = self._protector.protect(encode_config(normalized))

        pending = await self._credentials.start_credential_validation(
            team_id,
            clean_label,
            spec.name,
            protected,
            actor_id,
        )

        try:
            await preflight(spec.name, normalized)
        except ProxyConfigurationError as error:
            await self._credentials.finish_credential_validation(
                pending.id,
                state=CredentialState.FAILED,
                # Store only the stable failure code, never provider output.
                detail=Reason.PROXY_PREFLIGHT_FAILED.value,
                actor_id=actor_id,
            )

            raise CredentialConfigurationError(
                Reason.PROXY_PREFLIGHT_FAILED,
            ) from error

        credential = await self._credentials.finish_credential_validation(
            pending.id,
            state=CredentialState.ACTIVE,
            detail="",
            actor_id=actor_id,
        )

        await self._record(
            AuditAction.CREDENTIAL_CONFIGURED,
            actor_id,
            trace,
            team=team_id,
            credential=credential.id,
            provider=spec.name,
        )

        return credential

    async def _commit_totp(
        self,
        user: PortalUser,
        secret: str,
    ) -> tuple[str, ...] | None:
        first_factor = not user.has_second_factor
        recovery_codes = new_recovery_codes() if first_factor else None

        await self._auth.enable_totp(
            user.id,
            self._protector.protect(secret.encode("utf-8")),
            (
                None
                if recovery_codes is None
                else tuple(token_hash(code) for code in recovery_codes)
            ),
            promote_to_site_admin=user.pending_site_admin,
        )

        await self._record(AuditAction.MFA_ENROLLED, user.id, RequestTrace())

        return recovery_codes

    async def _consume_setup(
        self,
        purpose: str,
        setup_token: str,
        user_id: UUID,
    ) -> dict[str, str]:
        payload = await self._setup_tokens.consume(purpose, setup_token)
        return self._parse_setup(payload, user_id)

    async def _peek_setup(
        self,
        purpose: str,
        setup_token: str,
        user_id: UUID,
    ) -> dict[str, str]:
        payload = await self._setup_tokens.peek(purpose, setup_token)
        return self._parse_setup(payload, user_id)

    @staticmethod
    def _parse_setup(payload: str | None, user_id: UUID) -> dict[str, str]:
        if payload is None:
            raise ProvisioningError(Reason.SETUP_EXPIRED)

        pending: dict[str, str] = json.loads(payload)

        if pending["user_id"] != str(user_id):
            raise ProvisioningError(Reason.SETUP_EXPIRED)

        return pending

    async def _record(
        self,
        action: AuditAction,
        actor_id: UUID,
        trace: RequestTrace,
        **metadata: object,
    ) -> None:
        await self._audit.record(
            AuditEvent(
                action=action,
                actor_id=actor_id,
                trace=trace,
                metadata={key: str(value) for key, value in metadata.items()},
            )
        )

    async def _reload(self, user_id: UUID) -> PortalUser:
        user = await self._auth.user_by_id(user_id)

        if user is None:
            raise NotFound(Reason.USER_NOT_FOUND)

        return user

    @staticmethod
    def provider_fields(provider: str) -> tuple[Field, ...]:
        if provider not in PROVIDERS:
            raise CredentialConfigurationError(Reason.PROXY_UNAVAILABLE)

        return spec_for(provider).fields

    async def _require_site_admin(self, actor_id: UUID) -> PortalUser:
        user = await self._auth.user_by_id(actor_id)

        if user is None or not user.is_site_admin or not user.is_active:
            raise PermissionDenied(Reason.SITE_ADMIN_REQUIRED)

        return user

    async def _require_leader(self, actor_id: UUID, team_id: UUID) -> None:
        role = await self._teams.role_for(actor_id, team_id)

        if role is not TeamRole.TEAM_LEADER:
            raise PermissionDenied(Reason.LEADER_REQUIRED)

    async def _require_leader_or_site_admin(
        self, actor_id: UUID, team_id: UUID
    ) -> None:
        actor = await self._auth.user_by_id(actor_id)

        if actor is not None and actor.is_site_admin and actor.is_active:
            return

        await self._require_leader(actor_id, team_id)

    async def _guard_account_removal(
        self,
        actor_id: UUID,
        user_id: UUID,
    ) -> PortalUser:
        """Shared precondition for deactivate_user and delete_user: never act
        on your own account, never strip a team of its last leader.

        No app-level "last site admin" check: the actor must itself be an
        active site admin (site_admin_step_up) and can't target itself (just
        above), so a different, active admin target always leaves at least
        the actor standing. portal_installation_must_have_admin is the real
        backstop, for the concurrent-request case this can't reason about.
        """
        if user_id == actor_id:
            raise ProvisioningError(Reason.USER_CANNOT_DEACTIVATE_SELF)

        target = await self._auth.user_by_id(user_id)

        if target is None:
            raise NotFound(Reason.USER_NOT_FOUND)

        sole_leader_of = await self._teams.teams_where_sole_leader(user_id)

        if sole_leader_of:
            raise ProvisioningError(
                Reason.USER_LAST_LEADER,
                teams=", ".join(team.name for team in sole_leader_of),
            )

        return target

    async def _user_by_email(self, email: str) -> PortalUser:
        found = await self._auth.user_by_email(self._email(email))

        if found is None:
            raise NotFound(Reason.USER_NOT_FOUND)

        return found[0]

    @staticmethod
    def _name(value: str) -> str:
        name = value.strip()

        if not 1 <= len(name) <= 120:
            raise ProvisioningError(Reason.TEAM_NAME_LENGTH)

        return name

    @staticmethod
    def _slug(value: str) -> str:
        slug = value.strip().lower()

        if not _SLUG.fullmatch(slug):
            raise ProvisioningError(Reason.SLUG_INVALID)

        return slug

    @staticmethod
    def _slugify(name: str) -> str:
        """Derive a slug from a team name for the browser flow, where slug is
        a DB uniqueness detail nobody should have to type in. Capped at 58
        chars to leave room for a "-NN" disambiguator under _SLUG's 63-char
        limit; CLI provisioning still takes an explicit slug via _slug."""
        base = _SLUGIFY.sub("-", name.strip().lower()).strip("-")[:58]

        if len(base) < 2:
            base = f"{base}-equipo"[:58] if base else "equipo"

        return base

    async def _unique_slug(self, name: str) -> str:
        base = self._slugify(name)
        candidate = base
        suffix = 2

        while await self._teams.team_by_slug(candidate) is not None:
            candidate = f"{base}-{suffix}"
            suffix += 1

        return candidate

    @staticmethod
    def _email(value: str) -> str:
        email = value.lower().strip()

        if not _EMAIL.fullmatch(email):
            raise ProvisioningError(Reason.EMAIL_INVALID)

        return email

    @staticmethod
    def _label(value: str) -> str:
        label = value.strip()

        if not 1 <= len(label) <= 120:
            raise CredentialConfigurationError(Reason.LABEL_LENGTH)

        return label
