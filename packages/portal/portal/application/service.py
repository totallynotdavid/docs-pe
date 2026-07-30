from __future__ import annotations

from typing import TYPE_CHECKING

from portal.domain.errors import NotFound, PermissionDenied
from portal.domain.models import TeamRole
from portal.domain.planning import plan_submission


if TYPE_CHECKING:
    from uuid import UUID

    from portal.domain.models import Job, SubmitJob
    from portal.repository.protocols import PortalRepository


class PortalService:
    """Team-scoped commands. Repository methods provide transaction boundaries."""

    def __init__(self, repository: PortalRepository) -> None:
        self._repository = repository

    async def submit(self, command: SubmitJob) -> Job:
        role = await self._repository.role_for(command.actor_id, command.team_id)
        if role not in {TeamRole.SITE_ADMIN, TeamRole.TEAM_LEADER}:
            raise PermissionDenied("solo un líder del equipo puede crear procesos")
        credential = await self._repository.credential(command.credential_version_id)
        if credential is None or credential.team_id != command.team_id:
            raise PermissionDenied("la credencial debe pertenecer al mismo equipo")
        if not credential.is_active:
            raise PermissionDenied("la versión de credencial ya no está activa")
        return await self._repository.admit_submission(
            command, plan_submission(command.lines, command.sources)
        )

    async def cancel(self, actor_id: UUID, team_id: UUID, job_id: UUID) -> Job:
        role = await self._repository.role_for(actor_id, team_id)
        if role not in {TeamRole.SITE_ADMIN, TeamRole.TEAM_LEADER}:
            raise PermissionDenied("solo un líder del equipo puede cancelar procesos")
        job = await self._repository.cancel(job_id, team_id)
        if job is None:
            raise NotFound("proceso no encontrado en el equipo")
        return job

    async def published_results(self, actor_id: UUID, team_id: UUID) -> tuple[Job, ...]:
        if await self._repository.role_for(actor_id, team_id) is None:
            raise PermissionDenied("no pertenece al equipo")
        return await self._repository.published_jobs(team_id)
