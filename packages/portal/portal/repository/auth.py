from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from portal.domain.models import BrowserSession, PortalUser
from portal.repository.shared import user_row


if TYPE_CHECKING:
    from datetime import datetime

    from asyncpg import Pool


class PostgresAuthRepository:
    """Users, sessions, and login rate limiting."""

    def __init__(self, pool: Pool) -> None:
        self._pool = pool

    async def user_by_email(self, email: str) -> tuple[PortalUser, str] | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT id, email, is_site_admin, password_hash
                  FROM portal_users WHERE email = $1
                """,
                email.lower().strip(),
            )
        if row is None:
            return None
        return user_row(row), str(row["password_hash"])

    async def user_by_id(self, user_id: UUID) -> PortalUser | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT id, email, is_site_admin FROM portal_users WHERE id = $1",
                user_id,
            )
        return user_row(row) if row else None

    async def create_user(
        self, email: str, password_hash: str, *, is_site_admin: bool = False
    ) -> PortalUser:
        user = PortalUser(uuid4(), email.lower().strip(), is_site_admin)
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO portal_users (id, email, password_hash, is_site_admin)
                VALUES ($1, $2, $3, $4)
                """,
                user.id,
                user.email,
                password_hash,
                user.is_site_admin,
            )
        return user

    async def provision_site_admin(self, email: str, password_hash: str) -> PortalUser:
        """Create/find the declared initial administrator without changing a password."""
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO portal_users (id, email, password_hash, is_site_admin)
                VALUES ($1, $2, $3, true)
                ON CONFLICT (email) DO UPDATE
                    SET is_site_admin = true
                RETURNING id, email, is_site_admin
                """,
                uuid4(),
                email.lower().strip(),
                password_hash,
            )
        return user_row(row)

    async def create_session(
        self, user_id: UUID, token_hash: str, csrf_token: str, expires_at: datetime
    ) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO portal_sessions (id, user_id, token_hash, csrf_token, expires_at)
                VALUES ($1, $2, $3, $4, $5)
                """,
                uuid4(),
                user_id,
                token_hash,
                csrf_token,
                expires_at,
            )

    async def browser_session(
        self, token_hash: str, now: datetime
    ) -> BrowserSession | None:
        async with self._pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT user_account.id, user_account.email, user_account.is_site_admin,
                       session.csrf_token
                  FROM portal_sessions AS session
                  JOIN portal_users AS user_account ON user_account.id = session.user_id
                 WHERE session.token_hash = $1
                   AND session.expires_at > $2
                   AND session.csrf_token IS NOT NULL
                """,
                token_hash,
                now,
            )
            await connection.execute(
                "DELETE FROM portal_sessions WHERE expires_at <= $1", now
            )
        if row is None:
            return None
        return BrowserSession(user_row(row), str(row["csrf_token"]))

    async def destroy_session(self, token_hash: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM portal_sessions WHERE token_hash = $1", token_hash
            )

    async def issue_login_csrf(self, token: str, expires_at: datetime) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO portal_login_csrf_tokens (token, expires_at) VALUES ($1, $2)",
                token,
                expires_at,
            )

    async def consume_login_csrf(self, token: str, now: datetime) -> bool:
        async with self._pool.acquire() as connection:
            consumed = await connection.fetchval(
                """
                DELETE FROM portal_login_csrf_tokens
                 WHERE token = $1 AND expires_at > $2
                RETURNING token
                """,
                token,
                now,
            )
            await connection.execute(
                "DELETE FROM portal_login_csrf_tokens WHERE expires_at <= $1", now
            )
        return consumed is not None

    async def login_allowed(self, email: str, client_ip: str, now: datetime) -> bool:
        async with self._pool.acquire() as connection:
            failures = await connection.fetchval(
                """
                SELECT count(*) FROM portal_login_failures
                 WHERE email = $1 AND client_ip = $2
                   AND attempted_at > $3::timestamptz - interval '5 minutes'
                """,
                email.lower().strip(),
                client_ip,
                now,
            )
        return int(failures) < 5

    async def record_login_failure(
        self, email: str, client_ip: str, now: datetime
    ) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO portal_login_failures (email, client_ip, attempted_at)
                VALUES ($1, $2, $3)
                """,
                email.lower().strip(),
                client_ip,
                now,
            )

    async def clear_login_failures(self, email: str, client_ip: str) -> None:
        async with self._pool.acquire() as connection:
            await connection.execute(
                "DELETE FROM portal_login_failures WHERE email = $1 AND client_ip = $2",
                email.lower().strip(),
                client_ip,
            )
