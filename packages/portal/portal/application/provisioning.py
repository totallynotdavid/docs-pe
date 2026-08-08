from __future__ import annotations

import re

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
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
    MfaEnrollment,
    PortalUser,
    RequestTrace,
    Team,
    TeamRole,
)
from portal.security import (
    hash_password,
    new_recovery_codes,
    new_totp_secret,
    token_hash,
    totp_enrollment_uri,
)


if TYPE_CHECKING:
    from fetch.proxy.base import Field

    from portal.repository.audit import PostgresAuditLog
    from portal.repository.auth import PostgresAuthRepository
    from portal.repository.credentials import PostgresCredentialRepository
    from portal.repository.teams import PostgresTeamRepository

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MIN_PASSWORD = 12


@dataclass(frozen=True)
class InstallationStatus:
    team_count: int
    initial_team_id: UUID | None
    can_create_first_team: bool
    next_step: str


@dataclass(frozen=True)
class TeamReadiness:
    has_active_credential: bool
    next_step: str


@dataclass(frozen=True)
class FirstTeamResult:
    team: Team
    created: bool


class ProvisioningService(AuthorizedService):
    def __init__(
        self,
        auth: PostgresAuthRepository,
        teams: PostgresTeamRepository,
        credentials: PostgresCredentialRepository,
        protector: EnvelopeProtector,
        audit: PostgresAuditLog,
        issuer: str,
    ) -> None:
        self._auth = auth
        self._teams = teams
        self._credentials = credentials
        self._protector = protector
        self._audit = audit
        self._issuer = issuer

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
    ) -> tuple[PortalUser, MfaEnrollment | None]:
        """Create or verify the initial administrator.

        portal_site_admin_requires_mfa means the promotion and the second
        factor happen together. An account that already carries one keeps it:
        re-running provisioning must not silently invalidate the authenticator
        the administrator is holding.
        """
        user = await self._auth.create_account(email, password_hash)

        if user.mfa_enabled:
            return user, None

        enrollment = await self._enroll_mfa(user, promote_to_site_admin=True)

        return await self._reload(user.id), enrollment

    @site_admin
    async def create_first_team(
        self,
        actor_id: UUID,
        *,
        name: str,
        slug: str,
        trace: RequestTrace,
    ) -> Team:
        team = await self._teams.create_first_team(
            self._slug(slug),
            self._name(name),
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
        slug: str,
        leader_email: str,
        mfa_verified_at: datetime | None,
        trace: RequestTrace,
    ) -> Team:
        leader = await self._user_by_email(leader_email)

        team = await self._teams.create_team(
            self._slug(slug),
            self._name(name),
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
    ) -> MfaEnrollment | None:
        """Promotes and enrolls a second factor in one step.

        portal_site_admin_requires_mfa means is_site_admin can only become
        true alongside mfa_enabled, so this reuses _enroll_mfa (the same path
        bootstrap uses) rather than a bare UPDATE. Returns the enrollment
        (shown once, like bootstrap's) only when a new one was created.
        """
        user = await self._auth.user_by_id(user_id)

        if user is None:
            raise NotFound(Reason.USER_NOT_FOUND)

        if user.is_site_admin:
            return None

        enrollment = await self._enroll_mfa(user, promote_to_site_admin=True)

        await self._record(AuditAction.USER_PROMOTED, actor_id, trace, user=user_id)

        return enrollment

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
        if role not in {TeamRole.TEAM_LEADER, TeamRole.TEAM_MEMBER}:
            raise ProvisioningError(Reason.ROLE_INVALID)

        member = await self._user_by_email(email)

        await self._teams.add_member(team_id, member.id, role)

        await self._record(
            AuditAction.MEMBER_ADDED,
            actor_id,
            trace,
            team=team_id,
            user=member.id,
            role=role.value,
        )

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
    async def member_candidates(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> tuple[PortalUser, ...]:
        return await self._teams.users()

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

        return TeamReadiness(
            has_active_credential=has_active_credential,
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

    async def _enroll_mfa(
        self,
        user: PortalUser,
        *,
        promote_to_site_admin: bool,
    ) -> MfaEnrollment:
        secret = new_totp_secret()
        recovery_codes = new_recovery_codes()

        await self._auth.enable_mfa(
            user.id,
            self._protector.protect(secret.encode("utf-8")),
            tuple(token_hash(code) for code in recovery_codes),
            promote_to_site_admin=promote_to_site_admin,
        )

        await self._record(AuditAction.MFA_ENROLLED, user.id, RequestTrace())

        return MfaEnrollment(
            enrollment_uri=totp_enrollment_uri(
                secret,
                email=user.email,
                issuer=self._issuer,
            ),
            recovery_codes=recovery_codes,
        )

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
