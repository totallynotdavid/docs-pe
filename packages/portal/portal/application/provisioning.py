from __future__ import annotations

import re

from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from portal.credentials.providers import ProxyField, provider_for
from portal.credentials.secrets import SecretProtector
from portal.domain.errors import (
    CredentialConfigurationError,
    NotFound,
    PermissionDenied,
    ProvisioningError,
)
from portal.domain.models import (
    CredentialState,
    CredentialVersion,
    PortalUser,
    ProxyProvider,
    Team,
    TeamRole,
)
from portal.web.security import hash_password


_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


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


class ProvisioningService:
    """Installation, team setup, and proxy lifecycle application boundary."""

    def __init__(self, repository, secret_protector: SecretProtector) -> None:
        self._repository = repository
        self._secret_protector = secret_protector

    async def installation_status(self, actor_id: UUID) -> InstallationStatus:
        actor = await self._require_site_admin(actor_id)
        count, initial_team_id = await self._repository.installation_status()
        can_create = count == 0 and initial_team_id is None and actor.is_site_admin
        return InstallationStatus(
            team_count=count,
            initial_team_id=initial_team_id,
            can_create_first_team=can_create,
            next_step="crear_primer_equipo" if can_create else "administrar_sitio",
        )

    async def create_first_team(self, actor_id: UUID, *, name: str, slug: str) -> Team:
        await self._require_site_admin(actor_id)
        return await self._repository.create_first_team(
            self._slug(slug), self._name(name), actor_id
        )

    async def create_team(
        self, actor_id: UUID, *, name: str, slug: str, leader_email: str
    ) -> Team:
        await self._require_site_admin(actor_id)
        leader = await self._user_by_email(leader_email)
        return await self._repository.create_team(
            self._slug(slug), self._name(name), actor_id, leader.id
        )

    async def create_user(
        self, actor_id: UUID, *, email: str, password: str
    ) -> PortalUser:
        await self._require_site_admin(actor_id)
        if len(password) < 12:
            msg = "la contraseña debe tener al menos 12 caracteres"
            raise ProvisioningError(msg)
        return await self._repository.create_user(
            self._email(email), hash_password(password)
        )

    async def users(self, actor_id: UUID) -> tuple[PortalUser, ...]:
        await self._require_site_admin(actor_id)
        return await self._repository.users()

    async def teams(self, actor_id: UUID) -> tuple[Team, ...]:
        await self._require_site_admin(actor_id)
        return await self._repository.all_teams()

    async def invite_or_add_member(
        self, actor_id: UUID, *, team_id: UUID, email: str, role: TeamRole
    ) -> None:
        await self._require_leader(actor_id, team_id)
        if role not in {TeamRole.TEAM_LEADER, TeamRole.TEAM_MEMBER}:
            msg = "el rol seleccionado no es válido"
            raise ProvisioningError(msg)
        member = await self._user_by_email(email)
        await self._repository.add_member(team_id, member.id, role)

    async def remove_member(self, actor_id: UUID, *, team_id: UUID, email: str) -> None:
        await self._require_leader(actor_id, team_id)
        member = await self._user_by_email(email)
        await self._repository.remove_member(team_id, member.id)

    async def members(
        self, actor_id: UUID, team_id: UUID
    ) -> tuple[tuple[PortalUser, TeamRole], ...]:
        await self._require_leader(actor_id, team_id)
        return await self._repository.members_for_team(team_id)

    async def member_candidates(
        self, actor_id: UUID, team_id: UUID
    ) -> tuple[PortalUser, ...]:
        await self._require_leader(actor_id, team_id)
        return await self._repository.users()

    async def team_readiness(self, actor_id: UUID, team_id: UUID) -> TeamReadiness:
        await self._require_leader(actor_id, team_id)
        active = any(
            credential.is_active and credential.state is CredentialState.ACTIVE
            for credential in await self._repository.credentials_for_team(team_id)
        )
        return TeamReadiness(
            has_active_credential=active,
            next_step="enviar_trabajo" if active else "configurar_proxy",
        )

    async def configure_proxy(
        self,
        actor_id: UUID,
        *,
        team_id: UUID,
        label: str,
        provider: ProxyProvider | str,
        values: Mapping[str, str],
    ) -> CredentialVersion:
        await self._require_leader(actor_id, team_id)
        clean_label = self._label(label)
        adapter = provider_for(provider)
        normalized = adapter.normalize(values)
        protected = await self._secret_protector.protect(normalized)
        pending = await self._repository.start_credential_validation(
            team_id,
            clean_label,
            adapter.provider.value,
            protected.ciphertext,
            protected.key_id,
            actor_id,
        )
        try:
            await adapter.preflight(normalized)
        except CredentialConfigurationError:
            await self._repository.finish_credential_validation(
                pending.id,
                state=CredentialState.FAILED,
                detail="no se pudo validar la conexión con el proveedor",
                actor_id=actor_id,
            )
            raise
        return await self._repository.finish_credential_validation(
            pending.id,
            state=CredentialState.ACTIVE,
            detail="validación completada",
            actor_id=actor_id,
        )

    @staticmethod
    def provider_fields(provider: ProxyProvider | str) -> tuple[ProxyField, ...]:
        return provider_for(provider).fields

    async def _require_site_admin(self, actor_id: UUID) -> PortalUser:
        user = await self._repository.user_by_id(actor_id)
        if user is None or not user.is_site_admin:
            raise PermissionDenied("solo la administración del sitio puede continuar")
        return user

    async def _require_leader(self, actor_id: UUID, team_id: UUID) -> None:
        role = await self._repository.role_for(actor_id, team_id)
        if role is not TeamRole.TEAM_LEADER:
            raise PermissionDenied("solo un líder del equipo puede continuar")

    async def _user_by_email(self, email: str) -> PortalUser:
        found = await self._repository.user_by_email(self._email(email))
        if found is None:
            msg = "no se encontró una persona con ese correo"
            raise NotFound(msg)
        return found[0]

    @staticmethod
    def _name(value: str) -> str:
        name = value.strip()
        if not 1 <= len(name) <= 120:
            msg = "el nombre del equipo debe tener entre 1 y 120 caracteres"
            raise ProvisioningError(msg)
        return name

    @staticmethod
    def _slug(value: str) -> str:
        slug = value.strip().lower()
        if not _SLUG.fullmatch(slug):
            msg = "el identificador debe usar minúsculas, números y guiones"
            raise ProvisioningError(msg)
        return slug

    @staticmethod
    def _email(value: str) -> str:
        email = value.lower().strip()
        if not _EMAIL.fullmatch(email):
            msg = "el correo no tiene un formato válido"
            raise ProvisioningError(msg)
        return email

    @staticmethod
    def _label(value: str) -> str:
        label = value.strip()
        if not 1 <= len(label) <= 120:
            msg = "la etiqueta debe tener entre 1 y 120 caracteres"
            raise CredentialConfigurationError(msg)
        return label
