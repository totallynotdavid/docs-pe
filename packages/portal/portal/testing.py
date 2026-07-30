"""Test builders kept outside application modules so focused contracts stay concise."""

from __future__ import annotations

from uuid import UUID, uuid4

from portal.domain.models import CredentialVersion, InputLine, SubmitJob, TeamRole
from portal.repository.memory import InMemoryPortalRepository


def leader(
    repository: InMemoryPortalRepository, team_id: UUID | None = None
) -> tuple[UUID, UUID, UUID]:
    actor_id = uuid4()
    team_id = team_id or uuid4()
    credential_id = uuid4()
    repository.grant(actor_id, team_id, TeamRole.TEAM_LEADER)
    repository.add_credential(
        CredentialVersion(credential_id, team_id, "Proxy Perú", version=1)
    )
    return actor_id, team_id, credential_id


def command(
    actor_id: UUID,
    team_id: UUID,
    credential_version_id: UUID,
    *,
    value: str = "10412345678",
    sources: tuple[str, ...] = ("osiptel",),
) -> SubmitJob:
    return SubmitJob(
        actor_id=actor_id,
        team_id=team_id,
        credential_version_id=credential_version_id,
        input_object_id=uuid4(),
        filename="entrada.csv",
        sources=sources,
        lines=(InputLine(1, value),),
    )
