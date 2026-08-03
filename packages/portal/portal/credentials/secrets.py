from __future__ import annotations

import base64
import json
import os
import secrets

from binascii import Error as BinasciiError
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from portal.domain.errors import CredentialConfigurationError, Reason


NONCE_BYTES = 12


@dataclass(frozen=True)
class ProtectedSecret:
    ciphertext: bytes
    key_id: str


class AesGcmSecretProtector:
    """Protects stored proxy credentials with an environment-provided key."""

    key_id = "environment"

    def __init__(self, encoded_key: str) -> None:
        try:
            padding = "=" * (-len(encoded_key) % 4)
            key = base64.urlsafe_b64decode((encoded_key + padding).encode("ascii"))
        except (BinasciiError, UnicodeEncodeError, ValueError) as error:
            msg = "PORTAL_SECRET_PROTECTION_KEY must be urlsafe base64"
            raise RuntimeError(msg) from error

        if len(key) != 32:
            msg = "PORTAL_SECRET_PROTECTION_KEY must decode to 32 bytes"
            raise RuntimeError(msg)

        self._cipher = AESGCM(key)

    @classmethod
    def from_environment(cls) -> AesGcmSecretProtector:
        encoded_key = os.environ.get("PORTAL_SECRET_PROTECTION_KEY", "").strip()

        if not encoded_key:
            msg = "PORTAL_SECRET_PROTECTION_KEY is required"
            raise RuntimeError(msg)

        return cls(encoded_key)

    async def protect(self, values: dict[str, str]) -> ProtectedSecret:
        payload = json.dumps(
            values,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        nonce = secrets.token_bytes(NONCE_BYTES)
        ciphertext = self._cipher.encrypt(nonce, payload, None)

        return ProtectedSecret(
            ciphertext=nonce + ciphertext,
            key_id=self.key_id,
        )

    def reveal(self, ciphertext: bytes) -> dict[str, str]:
        if len(ciphertext) <= NONCE_BYTES:
            raise CredentialConfigurationError(Reason.PROXY_INVALID)

        nonce = ciphertext[:NONCE_BYTES]
        encrypted = ciphertext[NONCE_BYTES:]

        try:
            payload = self._cipher.decrypt(nonce, encrypted, None)
            values = json.loads(payload)
        except (ValueError, json.JSONDecodeError) as error:
            raise CredentialConfigurationError(Reason.PROXY_INVALID) from error

        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in values.items()
        ):
            raise CredentialConfigurationError(Reason.PROXY_INVALID)

        return values
