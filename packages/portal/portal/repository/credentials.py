from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from portal.domain.errors import CredentialConfigurationError, Reason
from portal.domain.models import CredentialState, CredentialVersion
from portal.repository.shared import lock_team_row


if TYPE_CHECKING:
    from asyncpg import Pool


class PostgresCredentialRepository:
    """Proxy credential versions and their validation lifecycle."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def credential(self, credential_version_id: UUID) -> CredentialVersion | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT version.id, version.team_id, credential.label,
                       version.version, version.is_active, version.lifecycle,
                       version.provider
                  FROM portal_team_proxy_credential_versions AS version
                  JOIN portal_team_proxy_credentials AS credential
                    ON credential.id = version.credential_id
                   AND credential.team_id = version.team_id
                 WHERE version.id = $1
                """,
                credential_version_id,
            )
        if row is None:
            return None
        return CredentialVersion(
            id=row["id"],
            team_id=row["team_id"],
            label=row["label"],
            version=int(row["version"]),
            is_active=bool(row["is_active"]),
        )

    async def credentials_for_team(
        self, team_id: UUID
    ) -> tuple[CredentialVersion, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT version.id, version.team_id, credential.label,
                       version.version, version.is_active, version.lifecycle,
                       version.provider
                  FROM portal_team_proxy_credential_versions AS version
                  JOIN portal_team_proxy_credentials AS credential
                    ON credential.id = version.credential_id
                 WHERE version.team_id = $1
                 ORDER BY credential.label, version.version DESC
                """,
                team_id,
            )
        return tuple(self._credential(row) for row in rows)

    async def start_credential_validation(
        self,
        team_id: UUID,
        label: str,
        provider: str,
        config_ciphertext: bytes,
        key_id: str,
        created_by: UUID,
    ) -> CredentialVersion:
        async with self._pool.acquire() as connection, connection.transaction():
            await lock_team_row(connection, team_id)
            credential_id = await connection.fetchval(
                """
                SELECT id FROM portal_team_proxy_credentials
                 WHERE team_id = $1 AND label = $2 FOR UPDATE
                """,
                team_id,
                label,
            )
            if credential_id is None:
                credential_id = uuid4()
                await connection.execute(
                    """
                    INSERT INTO portal_team_proxy_credentials (id, team_id, label, created_by)
                    VALUES ($1, $2, $3, $4)
                    """,
                    credential_id,
                    team_id,
                    label,
                    created_by,
                )
            version = int(
                await connection.fetchval(
                    """
                    SELECT COALESCE(max(version), 0) + 1
                      FROM portal_team_proxy_credential_versions
                     WHERE credential_id = $1
                    """,
                    credential_id,
                )
            )
            credential = CredentialVersion(
                uuid4(),
                team_id,
                label,
                version,
                is_active=False,
                state=CredentialState.VALIDATING,
                provider=provider,
            )
            await connection.execute(
                """
                INSERT INTO portal_team_proxy_credential_versions
                    (id, credential_id, team_id, version, provider, config_ciphertext,
                     key_id, lifecycle, is_active, created_by)
                VALUES ($1, $2, $3, $4, $5, $6, $7, 'validating', false, $8)
                """,
                credential.id,
                credential_id,
                team_id,
                credential.version,
                provider,
                config_ciphertext,
                key_id,
                created_by,
            )
            await connection.execute(
                """
                INSERT INTO portal_proxy_credential_events
                    (id, credential_version_id, from_lifecycle, to_lifecycle, detail, actor_id)
                VALUES ($1, $2, 'draft', 'validating', 'validación iniciada', $3)
                """,
                uuid4(),
                credential.id,
                created_by,
            )
        return credential

    async def finish_credential_validation(
        self,
        credential_version_id: UUID,
        *,
        state: CredentialState,
        detail: str | None,
        actor_id: UUID,
    ) -> CredentialVersion:
        if state not in {CredentialState.ACTIVE, CredentialState.FAILED}:
            raise CredentialConfigurationError(Reason.CREDENTIAL_STATE_INVALID)
        async with self._pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                """
                SELECT version.id, version.team_id, credential.id AS credential_id,
                       credential.label, version.version, version.is_active,
                       version.lifecycle, version.provider
                  FROM portal_team_proxy_credential_versions AS version
                  JOIN portal_team_proxy_credentials AS credential
                    ON credential.id = version.credential_id
                   AND credential.team_id = version.team_id
                 WHERE version.id = $1
                 FOR UPDATE OF credential, version
                """,
                credential_version_id,
            )
            if row is None or row["lifecycle"] != CredentialState.VALIDATING.value:
                raise CredentialConfigurationError(Reason.CREDENTIAL_NOT_PENDING)
            if state is CredentialState.ACTIVE:
                retired = await connection.fetch(
                    """
                    UPDATE portal_team_proxy_credential_versions
                       SET lifecycle = 'retired', is_active = false
                     WHERE credential_id = $1 AND lifecycle = 'active'
                    RETURNING id
                    """,
                    row["credential_id"],
                )
                await connection.executemany(
                    """
                    INSERT INTO portal_proxy_credential_events
                        (id, credential_version_id, from_lifecycle, to_lifecycle, detail, actor_id)
                    VALUES ($1, $2, 'active', 'retired', 'reemplazada por una nueva versión', $3)
                    """,
                    [(uuid4(), retired_row["id"], actor_id) for retired_row in retired],
                )
            await connection.execute(
                """
                UPDATE portal_team_proxy_credential_versions
                   SET lifecycle = $2, is_active = $3,
                       validated_at = CASE WHEN $2 = 'active' THEN now() ELSE validated_at END,
                       failure_detail = CASE WHEN $2 = 'failed' THEN $4 ELSE NULL END
                 WHERE id = $1
                """,
                credential_version_id,
                state.value,
                state is CredentialState.ACTIVE,
                detail,
            )
            await connection.execute(
                """
                INSERT INTO portal_proxy_credential_events
                    (id, credential_version_id, from_lifecycle, to_lifecycle, detail, actor_id)
                VALUES ($1, $2, 'validating', $3, $4, $5)
                """,
                uuid4(),
                credential_version_id,
                state.value,
                detail,
                actor_id,
            )
        return CredentialVersion(
            id=row["id"],
            team_id=row["team_id"],
            label=row["label"],
            version=int(row["version"]),
            is_active=state is CredentialState.ACTIVE,
            state=state,
            provider=row["provider"],
        )

    @staticmethod
    def _credential(row: object) -> CredentialVersion:
        return CredentialVersion(
            id=row["id"],  # type: ignore[index]
            team_id=row["team_id"],  # type: ignore[index]
            label=row["label"],  # type: ignore[index]
            version=int(row["version"]),  # type: ignore[index]
            is_active=bool(row["is_active"]),  # type: ignore[index]
            state=CredentialState(row["lifecycle"]),  # type: ignore[index]
            provider=row["provider"],  # type: ignore[index]
        )
