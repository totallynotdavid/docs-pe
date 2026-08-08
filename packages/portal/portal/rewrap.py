from __future__ import annotations

import asyncio

from typing import TYPE_CHECKING, NamedTuple

from portal.credentials.masterkey import MasterKeyring
from portal.settings import PortalSettings


if TYPE_CHECKING:
    from collections.abc import Sequence

    from asyncpg import Pool


class Rewrapped(NamedTuple):
    credentials: int
    mfa_secrets: int


async def rewrap(pool: Pool, keyring: MasterKeyring) -> Rewrapped:
    """Move every stored data key onto the active master key.

    Only the wrapped data key changes. Payload ciphertext is never read, never
    decrypted, and never rewritten, so this runs in one pass over small blobs
    however large the payloads are. Once it reports zero rows left behind, the
    superseded key can be deleted from the key file.

    Rows are updated one at a time on purpose: a partial run is safe, because
    every row still names the key that wraps it, and rerunning finishes the job.
    """
    return Rewrapped(
        credentials=await _rewrap_credentials(pool, keyring),
        mfa_secrets=await _rewrap_mfa_secrets(pool, keyring),
    )


async def _rewrap_credentials(pool: Pool, keyring: MasterKeyring) -> int:
    rows = await pool.fetch(
        """
        SELECT id, wrapped_data_key, master_key_version
          FROM portal_team_proxy_credential_versions
         WHERE master_key_version <> $1
        """,
        keyring.active_version,
    )

    for row in rows:
        await pool.execute(
            """
            UPDATE portal_team_proxy_credential_versions
               SET wrapped_data_key = $2,
                   master_key_version = $3
             WHERE id = $1
            """,
            row["id"],
            *_rewrapped(keyring, row["wrapped_data_key"], row["master_key_version"]),
        )

    return len(rows)


async def _rewrap_mfa_secrets(pool: Pool, keyring: MasterKeyring) -> int:
    rows = await pool.fetch(
        """
        SELECT id, mfa_secret_wrapped_data_key, mfa_secret_master_key_version
          FROM portal_users
         WHERE mfa_enabled
           AND mfa_secret_master_key_version <> $1
        """,
        keyring.active_version,
    )

    for row in rows:
        await pool.execute(
            """
            UPDATE portal_users
               SET mfa_secret_wrapped_data_key = $2,
                   mfa_secret_master_key_version = $3
             WHERE id = $1
            """,
            row["id"],
            *_rewrapped(
                keyring,
                row["mfa_secret_wrapped_data_key"],
                row["mfa_secret_master_key_version"],
            ),
        )

    return len(rows)


def _rewrapped(
    keyring: MasterKeyring,
    wrapped: bytes,
    key_version: str,
) -> tuple[bytes, str]:
    data_key = keyring.unwrap(bytes(wrapped), key_version)

    return keyring.rewrap(data_key), keyring.active_version


async def _run() -> None:
    import asyncpg

    settings = PortalSettings.from_environment()
    settings.validate()

    keyring = MasterKeyring.from_file(settings.master_key_file)
    pool = await asyncpg.create_pool(settings.database_dsn)

    try:
        moved = await rewrap(pool, keyring)
    finally:
        await pool.close()

    print(f"Active master key: {keyring.active_version}")
    print(f"Proxy credentials re-wrapped: {moved.credentials}")
    print(f"MFA secrets re-wrapped: {moved.mfa_secrets}")


def run(argv: Sequence[str]) -> None:
    if argv:
        raise SystemExit("portal rewrap takes no arguments")

    try:
        asyncio.run(_run())
    except Exception as error:
        raise SystemExit(f"Re-wrap did not complete: {error}") from error
