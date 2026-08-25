from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import asyncpg

from portal.domain.errors import NotFound, ProvisioningError, Reason
from portal.domain.models import PortalUser, ProtectedSecret, WebAuthnCredential
from portal.repository.shared import has_passkey_sql, user_row


if TYPE_CHECKING:
    from asyncpg import Connection, Pool, Record


def normalize_email(email: str) -> str:
    return email.lower().strip()


def _webauthn_credential_row(row: Record) -> WebAuthnCredential:
    return WebAuthnCredential(
        id=row["id"],
        user_id=row["user_id"],
        credential_id=bytes(row["credential_id"]),
        public_key=bytes(row["public_key"]),
        sign_count=int(row["sign_count"]),
        transports=tuple(row["transports"]),
        label=str(row["label"]),
        created_at=row["created_at"],
        last_used_at=row["last_used_at"],
    )


class PostgresAuthRepository:
    """Durable account state. Sessions and login counters live in EphemeralStore."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def user_by_email(self, email: str) -> tuple[PortalUser, str] | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                SELECT id, email, is_site_admin, is_active, mfa_enabled,
                       pending_site_admin, {has_passkey_sql()}, password_hash
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
                f"""
                SELECT id, email, is_site_admin, is_active, mfa_enabled,
                       pending_site_admin, {has_passkey_sql()}
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

        Promotion cannot happen until the account carries a second factor
        (portal_admin_requires_second_factor), so provisioning creates the
        account first and promotes it once enrollment completes.
        """
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                -- The no-op SET is what makes RETURNING fire on conflict, so
                -- rerunning provisioning reads the existing account back
                -- without a second query and without touching its password.
                INSERT INTO portal_users (id, email, password_hash)
                VALUES ($1, $2, $3)
                ON CONFLICT (email) DO UPDATE
                    SET email = EXCLUDED.email
                RETURNING id, email, is_site_admin, is_active, mfa_enabled,
                          pending_site_admin, {has_passkey_sql()}
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

    async def enable_totp(
        self,
        user_id: UUID,
        secret: ProtectedSecret,
        recovery_code_hashes: tuple[str, ...] | None,
        *,
        promote_to_site_admin: bool,
    ) -> None:
        """Commit an already-confirmed TOTP secret.

        recovery_code_hashes is None when the caller already holds an active
        second factor (adding TOTP alongside an existing passkey): the
        existing recovery set stays valid and is left untouched. Otherwise
        this is the user's first factor and the whole set is (re)issued.
        """
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """
                UPDATE portal_users
                   SET mfa_secret_ciphertext = $2,
                       mfa_secret_wrapped_data_key = $3,
                       mfa_secret_master_key_version = $4,
                       mfa_enabled = true,
                       is_site_admin = is_site_admin OR $5,
                       pending_site_admin = pending_site_admin AND NOT $5
                 WHERE id = $1
                """,
                user_id,
                secret.ciphertext,
                secret.wrapped_data_key,
                secret.master_key_version,
                promote_to_site_admin,
            )

            if recovery_code_hashes is not None:
                await self._replace_recovery_codes(
                    connection, user_id, recovery_code_hashes
                )

    async def disable_totp(self, user_id: UUID) -> None:
        """Reject via portal_admin_requires_second_factor if this is a site
        admin's last factor. Recovery codes only survive if a passkey does."""
        try:
            async with self._pool.acquire() as connection, connection.transaction():
                await connection.execute(
                    """
                    UPDATE portal_users
                       SET mfa_enabled = false,
                           mfa_secret_ciphertext = NULL,
                           mfa_secret_wrapped_data_key = NULL,
                           mfa_secret_master_key_version = NULL
                     WHERE id = $1
                    """,
                    user_id,
                )
                await connection.execute(
                    """
                    DELETE FROM portal_mfa_recovery_codes
                     WHERE user_id = $1
                       AND NOT EXISTS (
                           SELECT 1 FROM portal_webauthn_credentials WHERE user_id = $1
                       )
                    """,
                    user_id,
                )
        except asyncpg.exceptions.CheckViolationError as error:
            raise ProvisioningError(Reason.LAST_SECOND_FACTOR) from error

    async def add_webauthn_credential(
        self,
        user_id: UUID,
        *,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        transports: tuple[str, ...],
        label: str,
        recovery_code_hashes: tuple[str, ...] | None,
        promote_to_site_admin: bool,
    ) -> None:
        async with self._pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """
                INSERT INTO portal_webauthn_credentials
                    (id, user_id, credential_id, public_key, sign_count,
                     transports, label)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                """,
                uuid4(),
                user_id,
                credential_id,
                public_key,
                sign_count,
                list(transports),
                label,
            )

            if promote_to_site_admin:
                await connection.execute(
                    """
                    UPDATE portal_users
                       SET is_site_admin = true,
                           pending_site_admin = false
                     WHERE id = $1
                    """,
                    user_id,
                )

            if recovery_code_hashes is not None:
                await self._replace_recovery_codes(
                    connection, user_id, recovery_code_hashes
                )

    async def remove_webauthn_credential(
        self, user_id: UUID, credential_id: UUID
    ) -> None:
        try:
            async with self._pool.acquire() as connection, connection.transaction():
                deleted = await connection.execute(
                    """
                    DELETE FROM portal_webauthn_credentials
                     WHERE id = $1
                       AND user_id = $2
                    """,
                    credential_id,
                    user_id,
                )
        except asyncpg.exceptions.CheckViolationError as error:
            raise ProvisioningError(Reason.LAST_SECOND_FACTOR) from error

        if deleted == "DELETE 0":
            raise NotFound(Reason.PASSKEY_NOT_FOUND)

    async def webauthn_credentials(
        self, user_id: UUID
    ) -> tuple[WebAuthnCredential, ...]:
        async with self._pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT id, user_id, credential_id, public_key, sign_count,
                       transports, label, created_at, last_used_at
                  FROM portal_webauthn_credentials
                 WHERE user_id = $1
                 ORDER BY created_at
                """,
                user_id,
            )

        return tuple(_webauthn_credential_row(row) for row in rows)

    async def webauthn_credential_by_credential_id(
        self,
        credential_id: bytes,
    ) -> WebAuthnCredential | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT id, user_id, credential_id, public_key, sign_count,
                       transports, label, created_at, last_used_at
                  FROM portal_webauthn_credentials
                 WHERE credential_id = $1
                """,
                credential_id,
            )

        return _webauthn_credential_row(row) if row is not None else None

    async def touch_webauthn_credential(self, row_id: UUID, *, sign_count: int) -> bool:
        """Optimistic-concurrency counter bump: the WHERE clause is what turns
        a replayed/cloned assertion into a rejected write instead of a race.
        Many platform authenticators never advance past 0, which WebAuthn
        Level 2 says is not itself a sign of cloning, so 0-to-0 is allowed."""
        async with self._pool.acquire() as connection:
            updated = await connection.fetchval(
                """
                UPDATE portal_webauthn_credentials
                   SET sign_count = $2,
                       last_used_at = now()
                 WHERE id = $1
                   AND ($2 > sign_count OR ($2 = 0 AND sign_count = 0))
                RETURNING id
                """,
                row_id,
                sign_count,
            )

        return updated is not None

    async def promote_now(self, user_id: UUID) -> PortalUser:
        """The target already carries a qualifying factor: skip the pending
        state and promote directly."""
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                UPDATE portal_users
                   SET is_site_admin = true,
                       pending_site_admin = false
                 WHERE id = $1
                RETURNING id, email, is_site_admin, is_active, mfa_enabled,
                          pending_site_admin, {has_passkey_sql()}
                """,
                user_id,
            )

        if row is None:
            raise NotFound(Reason.USER_NOT_FOUND)

        return user_row(row)

    async def set_pending_site_admin(self, user_id: UUID, *, pending: bool) -> None:
        async with self._pool.acquire() as connection:
            updated = await connection.execute(
                "UPDATE portal_users SET pending_site_admin = $2 WHERE id = $1",
                user_id,
                pending,
            )

        if updated == "UPDATE 0":
            raise NotFound(Reason.USER_NOT_FOUND)

    @staticmethod
    async def _replace_recovery_codes(
        connection: Connection,
        user_id: UUID,
        recovery_code_hashes: tuple[str, ...],
    ) -> None:
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

    async def count_active_site_admins(self) -> int:
        return int(
            await self._pool.fetchval(
                "SELECT count(*) FROM portal_users WHERE is_site_admin AND is_active"
            )
        )

    async def deactivate(self, user_id: UUID, actor_id: UUID) -> PortalUser:
        """Idempotent: deactivating an already-inactive account just confirms it."""
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                UPDATE portal_users
                   SET is_active = false,
                       deactivated_at = COALESCE(deactivated_at, now()),
                       deactivated_by = COALESCE(deactivated_by, $2)
                 WHERE id = $1
                RETURNING id, email, is_site_admin, is_active, mfa_enabled,
                          pending_site_admin, {has_passkey_sql()}
                """,
                user_id,
                actor_id,
            )

        if row is None:
            raise NotFound(Reason.USER_NOT_FOUND)

        return user_row(row)

    async def reactivate(self, user_id: UUID) -> PortalUser:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                UPDATE portal_users
                   SET is_active = true,
                       deactivated_at = NULL,
                       deactivated_by = NULL
                 WHERE id = $1
                RETURNING id, email, is_site_admin, is_active, mfa_enabled,
                          pending_site_admin, {has_passkey_sql()}
                """,
                user_id,
            )

        if row is None:
            raise NotFound(Reason.USER_NOT_FOUND)

        return user_row(row)

    async def demote(self, user_id: UUID) -> PortalUser:
        """Revoke site-admin status, or cancel a promotion still pending the
        user's own enrollment. Promotion itself lives in ProvisioningService,
        since it must also enroll a second factor before is_site_admin can
        become true (portal_admin_requires_second_factor)."""
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                f"""
                UPDATE portal_users
                   SET is_site_admin = false,
                       pending_site_admin = false
                 WHERE id = $1
                RETURNING id, email, is_site_admin, is_active, mfa_enabled,
                          pending_site_admin, {has_passkey_sql()}
                """,
                user_id,
            )

        if row is None:
            raise NotFound(Reason.USER_NOT_FOUND)

        return user_row(row)

    async def set_password(self, user_id: UUID, password_hash: str) -> None:
        async with self._pool.acquire() as connection:
            updated = await connection.execute(
                "UPDATE portal_users SET password_hash = $2 WHERE id = $1",
                user_id,
                password_hash,
            )

        if updated == "UPDATE 0":
            raise NotFound(Reason.USER_NOT_FOUND)

    async def delete_if_unused(self, user_id: UUID) -> bool:
        """True if the account had no history and could be removed outright.

        Every created_by/submitted_by/actor_id column that points at
        portal_users is a plain RESTRICT foreign key, so letting the database
        reject the DELETE is the only check that can never drift out of sync
        with what actually references a user.
        """
        async with self._pool.acquire() as connection:
            try:
                deleted = await connection.fetchval(
                    "DELETE FROM portal_users WHERE id = $1 RETURNING id",
                    user_id,
                )
            except asyncpg.exceptions.ForeignKeyViolationError:
                return False

        return deleted is not None
