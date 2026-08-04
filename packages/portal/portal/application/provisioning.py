from __future__ import annotations

import re

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from fetch.domain.errors import ProxyConfigurationError
from fetch.proxy.registry import PROVIDERS, preflight, spec_for

from portal.credentials.secrets import AesGcmSecretProtector
from portal.domain.errors import (
    CredentialConfigurationError,
    NotFound,
    PermissionDenied,
    ProvisioningError,
    Reason,
)
from portal.domain.models import (
    CredentialState,
    CredentialVersion,
    PortalUser,
    Team,
    TeamRole,
)
from portal.security import hash_password


if TYPE_CHECKING:
    from fetch.proxy.base import Field

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


class ProvisioningService:
    def __init__(
        self,
        auth: PostgresAuthRepository,
        teams: PostgresTeamRepository,
        credentials: PostgresCredentialRepository,
        secret_protector: AesGcmSecretProtector,
    ) -> None:
        self._auth = auth
        self._teams = teams
        self._credentials = credentials
        self._secret_protector = secret_protector

    async def installation_status(self, actor_id: UUID) -> InstallationStatus:
        await self._require_site_admin(actor_id)

        team_count, initial_team_id = await self._teams.installation_status()
        can_create_first_team = team_count == 0 and initial_team_id is None

        return InstallationStatus(
            team_count=team_count,
            initial_team_id=initial_team_id,
            can_create_first_team=can_create_first_team,
            next_step=(
                "crear_primer_equipo" if can_create_first_team else "administrar_sitio"
            ),
        )

    async def create_first_team(
        self,
        actor_id: UUID,
        *,
        name: str,
        slug: str,
    ) -> Team:
        await self._require_site_admin(actor_id)

        return await self._teams.create_first_team(
            self._slug(slug),
            self._name(name),
            actor_id,
        )

    async def ensure_first_team(
        self,
        actor_id: UUID,
        *,
        name: str,
        slug: str,
    ) -> FirstTeamResult:
        """Rerunning bootstrap against an existing installation verifies it."""
        await self._require_site_admin(actor_id)

        normalized_slug = self._slug(slug)
        team_count, initial_team_id = await self._teams.installation_status()

        if team_count == 0:
            team = await self._teams.create_first_team(
                normalized_slug,
                self._name(name),
                actor_id,
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

    async def create_team(
        self,
        actor_id: UUID,
        *,
        name: str,
        slug: str,
        leader_email: str,
    ) -> Team:
        await self._require_site_admin(actor_id)

        leader = await self._user_by_email(leader_email)

        return await self._teams.create_team(
            self._slug(slug),
            self._name(name),
            actor_id,
            leader.id,
        )

    async def create_user(
        self,
        actor_id: UUID,
        *,
        email: str,
        password: str,
    ) -> PortalUser:
        await self._require_site_admin(actor_id)

        if len(password) < _MIN_PASSWORD:
            raise ProvisioningError(
                Reason.PASSWORD_TOO_SHORT,
                minimum=_MIN_PASSWORD,
            )

        return await self._auth.create_user(
            self._email(email),
            hash_password(password),
        )

    async def users(self, actor_id: UUID) -> tuple[PortalUser, ...]:
        await self._require_site_admin(actor_id)

        return await self._teams.users()

    async def teams(self, actor_id: UUID) -> tuple[Team, ...]:
        await self._require_site_admin(actor_id)

        return await self._teams.all_teams()

    async def invite_or_add_member(
        self,
        actor_id: UUID,
        *,
        team_id: UUID,
        email: str,
        role: TeamRole,
    ) -> None:
        await self._require_leader(actor_id, team_id)

        if role not in {TeamRole.TEAM_LEADER, TeamRole.TEAM_MEMBER}:
            raise ProvisioningError(Reason.ROLE_INVALID)

        member = await self._user_by_email(email)

        await self._teams.add_member(team_id, member.id, role)

    async def remove_member(
        self,
        actor_id: UUID,
        *,
        team_id: UUID,
        email: str,
    ) -> None:
        await self._require_leader(actor_id, team_id)

        member = await self._user_by_email(email)

        await self._teams.remove_member(team_id, member.id)

    async def members(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> tuple[tuple[PortalUser, TeamRole], ...]:
        await self._require_leader(actor_id, team_id)

        return await self._teams.members_for_team(team_id)

    async def member_candidates(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> tuple[PortalUser, ...]:
        await self._require_leader(actor_id, team_id)

        return await self._teams.users()

    async def team_readiness(
        self,
        actor_id: UUID,
        team_id: UUID,
    ) -> TeamReadiness:
        await self._require_leader(actor_id, team_id)

        credentials = await self._credentials.credentials_for_team(team_id)
        has_active_credential = any(
            credential.is_active and credential.state is CredentialState.ACTIVE
            for credential in credentials
        )

        return TeamReadiness(
            has_active_credential=has_active_credential,
            next_step=(
                "enviar_trabajo" if has_active_credential else "configurar_proxy"
            ),
        )

    async def configure_proxy(
        self,
        actor_id: UUID,
        *,
        team_id: UUID,
        label: str,
        provider: str,
        values: Mapping[str, str],
    ) -> CredentialVersion:
        await self._require_leader(actor_id, team_id)

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

        protected = await self._secret_protector.protect(normalized)

        pending = await self._credentials.start_credential_validation(
            team_id,
            clean_label,
            spec.name,
            protected.ciphertext,
            protected.key_id,
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

        return await self._credentials.finish_credential_validation(
            pending.id,
            state=CredentialState.ACTIVE,
            detail="",
            actor_id=actor_id,
        )

    @staticmethod
    def provider_fields(provider: str) -> tuple[Field, ...]:
        if provider not in PROVIDERS:
            raise CredentialConfigurationError(Reason.PROXY_UNAVAILABLE)

        return spec_for(provider).fields

    async def _require_site_admin(self, actor_id: UUID) -> PortalUser:
        user = await self._auth.user_by_id(actor_id)

        if user is None or not user.is_site_admin:
            raise PermissionDenied(Reason.SITE_ADMIN_REQUIRED)

        return user

    async def _require_leader(self, actor_id: UUID, team_id: UUID) -> None:
        role = await self._teams.role_for(actor_id, team_id)

        if role is not TeamRole.TEAM_LEADER:
            raise PermissionDenied(Reason.LEADER_REQUIRED)

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
