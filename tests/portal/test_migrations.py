from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import asyncpg
import pytest

from portal.migrations import MIGRATIONS_DIR, apply_migrations


if TYPE_CHECKING:
    from tests.portal.conftest import PortalDatabase


async def test_applying_the_schema_twice_changes_nothing(
    unmigrated_db: PortalDatabase,
) -> None:
    """The ledger, not the SQL, is what makes a second run safe.

    None of the statements in the schema are idempotent on their own, so a
    runner that lost track of what it had applied would fail on the first
    CREATE TABLE rather than quietly doing damage. This asserts the ledger.
    """
    await apply_migrations(unmigrated_db.pool)
    await apply_migrations(unmigrated_db.pool)

    applied = await unmigrated_db.pool.fetchval(
        "SELECT count(*) FROM portal_schema_migrations"
    )

    assert applied == len(list(MIGRATIONS_DIR.glob("*.sql")))


async def test_a_site_admin_cannot_exist_without_a_second_factor(
    portal_db: PortalDatabase,
) -> None:
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await portal_db.pool.execute(
            """
            INSERT INTO portal_users (id, email, password_hash, is_site_admin)
            VALUES ($1, 'admin@example.test', 'x', true)
            """,
            uuid4(),
        )


async def test_a_passkey_alone_satisfies_the_site_admin_invariant(
    portal_db: PortalDatabase,
) -> None:
    """A passkey satisfies the site-admin second-factor constraint."""
    pool = portal_db.pool
    user_id = uuid4()

    await pool.execute(
        "INSERT INTO portal_users (id, email, password_hash) VALUES ($1, $2, 'x')",
        user_id,
        "passkey-admin@example.test",
    )
    await pool.execute(
        """
        INSERT INTO portal_webauthn_credentials
            (id, user_id, credential_id, public_key, label)
        VALUES ($1, $2, $3, $4, 'llave')
        """,
        uuid4(),
        user_id,
        b"\xde\xad\xbe\xef",
        b"\xca\xfe\xba\xbe",
    )

    # A passkey alone is enough: promoting does not need mfa_enabled too.
    await pool.execute(
        "UPDATE portal_users SET is_site_admin = true WHERE id = $1", user_id
    )

    # Removing that one passkey now must be rejected: it is this admin's
    # last factor.
    with pytest.raises(asyncpg.exceptions.CheckViolationError):
        await pool.execute(
            "DELETE FROM portal_webauthn_credentials WHERE user_id = $1", user_id
        )


async def test_a_stored_credential_must_carry_its_envelope(
    portal_db: PortalDatabase,
) -> None:
    """No row can hold ciphertext nothing is able to open."""
    user_id, team_id, credential_id = uuid4(), uuid4(), uuid4()
    pool = portal_db.pool

    await pool.execute(
        """
        INSERT INTO portal_users (id, email, password_hash)
        VALUES ($1, 'lider@example.test', 'x')
        """,
        user_id,
    )
    await pool.execute(
        """
        INSERT INTO portal_teams (id, slug, name, created_by)
        VALUES ($1, 'equipo-prueba', 'Equipo', $2)
        """,
        team_id,
        user_id,
    )
    await pool.execute(
        """
        INSERT INTO portal_team_proxy_credentials (id, team_id, label, created_by)
        VALUES ($1, $2, 'Proxy', $3)
        """,
        credential_id,
        team_id,
        user_id,
    )

    with pytest.raises(asyncpg.exceptions.NotNullViolationError):
        await pool.execute(
            """
            INSERT INTO portal_team_proxy_credential_versions
                (id, credential_id, team_id, version, provider,
                 config_ciphertext, lifecycle, is_active, created_by)
            VALUES ($1, $2, $3, 1, 'geonode', 'cifrado', 'active', true, $4)
            """,
            uuid4(),
            credential_id,
            team_id,
            user_id,
        )


async def test_the_audit_log_refuses_to_be_rewritten(
    portal_db: PortalDatabase,
) -> None:
    pool = portal_db.pool

    await pool.execute(
        """
        INSERT INTO portal_audit_log (id, action)
        VALUES ($1, 'login.succeeded')
        """,
        uuid4(),
    )

    for statement in (
        "UPDATE portal_audit_log SET action = 'login.failed'",
        "DELETE FROM portal_audit_log",
    ):
        with pytest.raises(asyncpg.exceptions.RaiseError, match="immutable"):
            await pool.execute(statement)
