from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from portal.domain.models import PortalUser, ProtectedSecret
from portal.repository.shared import user_row


if TYPE_CHECKING:
    from asyncpg import Pool


def normalize_email(email: str) -> str:
    return email.lower().strip()


class PostgresAuthRepository:
    """Durable account state. Sessions and login counters live in EphemeralStore."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def user_by_email(self, email: str) -> tuple[PortalUser, str] | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT id, email, is_site_admin, mfa_enabled, password_hash
                  FROM portal_users
                 WHERE email = $1
                """,
                normalize_email(email),
            )

        if row is None:
            return None

        return user_row(row), str(row["password_hash"])

    async def user_by_id(self, user_id: UUID) -> PortalUser | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT id, email, is_site_admin, mfa_enabled
                  FROM portal_users
                 WHERE id = $1
                """,
                user_id,
            )

        if row is None:
            return None

        return user_row(row)

    async def create_user(
        self,
        email: str,
        password_hash: str,
        *,
        is_site_admin: bool = False,
    ) -> PortalUser:
        user = PortalUser(uuid4(), normalize_email(email), is_site_admin)

        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO portal_users (
                    id,
                    email,
                    password_hash,
                    is_site_admin
                )
                VALUES ($1, $2, $3, $4)
                """,
                user.id,
                user.email,
                password_hash,
                user.is_site_admin,
            )

        return user

    async def create_account(self, email: str, password_hash: str) -> PortalUser:
        """Create the account an administrator will be promoted from.

        portal_site_admin_requires_mfa means promotion cannot happen until a
        TOTP secret is enrolled, so provisioning creates the account first and
        promotes it in enroll_mfa().
        """
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                -- The no-op SET is what makes RETURNING fire on conflict, so
                -- rerunning provisioning reads the existing account back
                -- without a second query and without touching its password.
                INSERT INTO portal_users (id, email, password_hash)
                VALUES ($1, $2, $3)
                ON CONFLICT (email) DO UPDATE
                    SET email = EXCLUDED.email
                RETURNING id, email, is_site_admin, mfa_enabled
                """,
                uuid4(),
                normalize_email(email),
                password_hash,
            )

        return user_row(row)

    async def mfa_secret(self, user_id: UUID) -> ProtectedSecret | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT mfa_secret_ciphertext,
                       mfa_secret_wrapped_data_key,
                       mfa_secret_master_key_version
                  FROM portal_users
                 WHERE id = $1
                   AND mfa_enabled
                """,
                user_id,
            )

        if row is None:
            return None

        return ProtectedSecret(
            ciphertext=bytes(row["mfa_secret_ciphertext"]),
            wrapped_data_key=bytes(row["mfa_secret_wrapped_data_key"]),
            master_key_version=str(row["mfa_secret_master_key_version"]),
        )

    async def enable_mfa(
        self,
        user_id: UUID,
        secret: ProtectedSecret,
        recovery_code_hashes: tuple[str, ...],
        *,
        promote_to_site_admin: bool,
    ) -> None:
        """Replace the secret and the whole recovery set in one transaction."""
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """
                UPDATE portal_users
                   SET mfa_secret_ciphertext = $2,
                       mfa_secret_wrapped_data_key = $3,
                       mfa_secret_master_key_version = $4,
                       mfa_enabled = true,
                       is_site_admin = is_site_admin OR $5
                 WHERE id = $1
                """,
                user_id,
                secret.ciphertext,
                secret.wrapped_data_key,
                secret.master_key_version,
                promote_to_site_admin,
            )

            await connection.execute(
                "DELETE FROM portal_mfa_recovery_codes WHERE user_id = $1",
                user_id,
            )

            await connection.executemany(
                """
                INSERT INTO portal_mfa_recovery_codes (id, user_id, code_hash)
                VALUES ($1, $2, $3)
                """,
                [(uuid4(), user_id, code) for code in recovery_code_hashes],
            )

    async def consume_recovery_code(self, user_id: UUID, code_hash: str) -> bool:
        """Spend one unused code. The WHERE clause is what makes it single-use."""
        async with self._pool.acquire() as connection:
            spent = await connection.fetchval(
                """
                UPDATE portal_mfa_recovery_codes
                   SET used_at = now()
                 WHERE user_id = $1
                   AND code_hash = $2
                   AND used_at IS NULL
                RETURNING id
                """,
                user_id,
                code_hash,
            )

        return spent is not None
