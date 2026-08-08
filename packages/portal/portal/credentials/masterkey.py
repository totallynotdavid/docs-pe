from __future__ import annotations

import base64
import secrets

from binascii import Error as BinasciiError
from dataclasses import dataclass
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path


DATA_KEY_BYTES = 32
NONCE_BYTES = 12

_VERSION_ALPHABET = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
)


@dataclass(frozen=True)
class DataKey:
    """A single-use key: `plaintext` encrypts one payload, `wrapped` is stored."""

    plaintext: bytes
    wrapped: bytes
    key_version: str


class MasterKeyring:
    """The keys that wrap data keys, loaded once from a file on disk.

    The file holds one key per line as `<version> <urlsafe-base64 32 bytes>`.
    The first line is the active key and wraps every new data key; the rest stay
    loaded so payloads written before a rotation can still be opened. Rotation
    is therefore: prepend a line, restart, re-wrap stored data keys at leisure,
    then drop the old line. It never touches payload ciphertext, which is the
    whole reason the envelope is worth having.

    The key comes from a file rather than the environment because on a container
    host the environment is not private: it appears in `docker inspect`, in the
    deployment UI, in /proc/<pid>/environ, and it is inherited by every
    subprocess, which here includes the fetch process running site code.

    What this protects is a leaked database dump or backup archive, since the
    key is in neither. What it cannot protect against is someone who already has
    the host, because the host must be able to read the key to serve a request.
    A hosted key service would narrow that case and add a per-decrypt audit
    trail; neither is reachable on a single-host deployment where the
    application and the database share a machine.
    """

    def __init__(self, active_version: str, keys: dict[str, AESGCM]) -> None:
        self._active_version = active_version
        self._keys = keys

    @classmethod
    def from_file(cls, path: Path) -> MasterKeyring:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as error:
            msg = f"PORTAL_MASTER_KEY_FILE is unreadable at {path}: {error}"
            raise RuntimeError(msg) from error

        return cls.from_lines(text.splitlines(), source=str(path))

    @classmethod
    def from_lines(
        cls, lines: Iterable[str], source: str = "the keyring"
    ) -> MasterKeyring:
        keys: dict[str, AESGCM] = {}
        order: list[str] = []

        for number, line in enumerate(lines, start=1):
            entry = line.strip()

            if not entry or entry.startswith("#"):
                continue

            version, key = _parse_key_line(entry, source, number)

            if version in keys:
                msg = f"{source} line {number}: key version {version} is repeated"
                raise RuntimeError(msg)

            keys[version] = AESGCM(key)
            order.append(version)

        if not order:
            msg = f"{source} holds no master keys. Write one with `portal new-key`."
            raise RuntimeError(msg)

        return cls(order[0], keys)

    @property
    def active_version(self) -> str:
        return self._active_version

    def generate_data_key(self) -> DataKey:
        plaintext = secrets.token_bytes(DATA_KEY_BYTES)

        return DataKey(
            plaintext=plaintext,
            wrapped=self.rewrap(plaintext),
            key_version=self._active_version,
        )

    def rewrap(self, data_key: bytes) -> bytes:
        """Wrap an existing data key under the active master key."""
        nonce = secrets.token_bytes(NONCE_BYTES)
        active = self._keys[self._active_version]

        return nonce + active.encrypt(nonce, data_key, None)

    def unwrap(self, wrapped: bytes, key_version: str) -> bytes:
        key = self._keys.get(key_version)

        if key is None:
            msg = (
                f"master key version {key_version} is not in the keyring, so the "
                "secrets it wrapped cannot be read. Restore that line before "
                "retiring it."
            )
            raise RuntimeError(msg)

        if len(wrapped) <= NONCE_BYTES:
            msg = "wrapped data key is truncated"
            raise ValueError(msg)

        return key.decrypt(wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:], None)


def new_master_key_line(version: str) -> str:
    """One keyring line, ready to prepend to the key file."""
    key = base64.urlsafe_b64encode(secrets.token_bytes(DATA_KEY_BYTES))

    return f"{version} {key.decode('ascii')}"


def _parse_key_line(entry: str, source: str, number: int) -> tuple[str, bytes]:
    parts = entry.split()

    if len(parts) != 2:
        msg = f"{source} line {number}: expected `<version> <urlsafe-base64 key>`"
        raise RuntimeError(msg)

    version, encoded = parts

    if not set(version) <= _VERSION_ALPHABET:
        msg = f"{source} line {number}: version may only use letters, digits, and -_."
        raise RuntimeError(msg)

    return version, _decode_key(encoded, source, number)


def _decode_key(encoded: str, source: str, number: int) -> bytes:
    try:
        padding = "=" * (-len(encoded) % 4)
        key = base64.urlsafe_b64decode((encoded + padding).encode("ascii"))
    except (BinasciiError, UnicodeEncodeError, ValueError) as error:
        msg = f"{source} line {number}: key must be urlsafe base64"
        raise RuntimeError(msg) from error

    if len(key) != DATA_KEY_BYTES:
        msg = f"{source} line {number}: key must decode to {DATA_KEY_BYTES} bytes"
        raise RuntimeError(msg)

    return key
