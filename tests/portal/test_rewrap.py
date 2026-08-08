from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from portal.credentials.masterkey import MasterKeyring, new_master_key_line
from portal.credentials.secrets import (
    EnvelopeProtector,
    decode_config,
    encode_config,
)
from portal.domain.models import ProtectedSecret
from portal.repository.auth import PostgresAuthRepository
from portal.rewrap import rewrap
from portal.security import token_hash

from tests.portal.conftest import (
    KEYRING,
    MASTER_KEY,
    MASTER_KEY_VERSION,
    PASSWORD,
    RECOVERY_CODE,
    TOTP_SECRET,
    seed_team,
)


if TYPE_CHECKING:
    import asyncpg


VALUES = {"username": "equipo", "password": "clave-del-proxy"}


@pytest.fixture
def rotated_lines() -> tuple[str, str]:
    """A key file mid-rotation: the new key first, the old one still present."""
    return (new_master_key_line("v2"), f"{MASTER_KEY_VERSION} {MASTER_KEY}")


@pytest.fixture
def rotated(rotated_lines: tuple[str, str]) -> MasterKeyring:
    return MasterKeyring.from_lines(rotated_lines)


async def test_a_rewrap_moves_stored_secrets_onto_the_active_key(
    pool: asyncpg.Pool,
    rotated: MasterKeyring,
) -> None:
    old = EnvelopeProtector(KEYRING)
    team = await seed_team(pool, config=old.protect(encode_config(VALUES)))

    ciphertext_before = await pool.fetchval(
        "SELECT config_ciphertext FROM portal_team_proxy_credential_versions"
    )

    moved = await rewrap(pool, rotated)

    assert moved.credentials == 1

    row = await pool.fetchrow(
        """
        SELECT config_ciphertext, master_key_version
          FROM portal_team_proxy_credential_versions
         WHERE id = $1
        """,
        team.credential_id,
    )

    assert row is not None
    assert row["master_key_version"] == "v2"

    # The payload is untouched, which is the property that makes a rotation
    # cheap: only the wrapped data key is rewritten.
    assert bytes(row["config_ciphertext"]) == bytes(ciphertext_before)


async def test_a_rewrapped_secret_opens_once_the_old_key_is_deleted(
    pool: asyncpg.Pool,
    rotated: MasterKeyring,
    rotated_lines: tuple[str, str],
) -> None:
    """The point of the whole exercise: the retired key can actually go away."""
    old = EnvelopeProtector(KEYRING)
    await seed_team(pool, config=old.protect(encode_config(VALUES)))

    await rewrap(pool, rotated)

    row = await pool.fetchrow(
        """
        SELECT config_ciphertext, wrapped_data_key, master_key_version
          FROM portal_team_proxy_credential_versions
        """
    )
    assert row is not None

    # The key file after the old line is deleted: only v2 remains.
    finished = EnvelopeProtector(MasterKeyring.from_lines([rotated_lines[0]]))
    secret = ProtectedSecret(
        ciphertext=bytes(row["config_ciphertext"]),
        wrapped_data_key=bytes(row["wrapped_data_key"]),
        master_key_version=str(row["master_key_version"]),
    )

    assert decode_config(finished.reveal(secret)) == VALUES


async def test_mfa_secrets_are_rewrapped_too(
    pool: asyncpg.Pool,
    rotated: MasterKeyring,
) -> None:
    auth = PostgresAuthRepository(pool)
    user = await auth.create_account("admin@osiptel.test", PASSWORD)

    await auth.enable_totp(
        user.id,
        EnvelopeProtector(KEYRING).protect(TOTP_SECRET.encode("utf-8")),
        (token_hash(RECOVERY_CODE),),
        promote_to_site_admin=True,
    )

    moved = await rewrap(pool, rotated)

    assert moved.mfa_secrets == 1

    secret = await auth.mfa_secret(user.id)

    assert secret is not None
    assert secret.master_key_version == "v2"
    assert EnvelopeProtector(rotated).reveal(secret).decode() == TOTP_SECRET


async def test_a_second_rewrap_finds_nothing_left_to_do(
    pool: asyncpg.Pool,
    rotated: MasterKeyring,
) -> None:
    await seed_team(pool, config=EnvelopeProtector(KEYRING).protect(b"clave"))

    await rewrap(pool, rotated)

    assert await rewrap(pool, rotated) == (0, 0)
