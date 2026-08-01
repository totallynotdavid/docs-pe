from __future__ import annotations

import base64
import json
import os
import secrets

from binascii import Error as BinasciiError
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from portal.domain.errors import CredentialConfigurationError, Reason


@dataclass(frozen=True)
class ProtectedSecret:
    """Opaque storage material; neither field is exposed by a browser read model."""

    ciphertext: bytes
    key_id: str


class AesGcmSecretProtector:
    """AES-GCM protection for stored proxy credentials, keyed from the environment.

    The key is never persisted with the ciphertext, returned to a caller, or
    rendered. A missing or malformed key is a deployment fault raised at startup,
    not a condition to degrade into: without it the portal cannot store a
    credential at all.
    """

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
        value = os.environ.get("PORTAL_SECRET_PROTECTION_KEY", "").strip()
        if not value:
            msg = "PORTAL_SECRET_PROTECTION_KEY is required"
            raise RuntimeError(msg)
        return cls(value)

    async def protect(self, values: dict[str, str]) -> ProtectedSecret:
        payload = json.dumps(values, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        nonce = secrets.token_bytes(12)
        return ProtectedSecret(
            ciphertext=nonce + self._cipher.encrypt(nonce, payload, None),
            key_id=self.key_id,
        )

    def reveal(self, ciphertext: bytes) -> dict[str, str]:
        """Decrypt a credential for the trusted worker control plane only."""
        try:
            payload = self._cipher.decrypt(ciphertext[:12], ciphertext[12:], None)
            values = json.loads(payload)
        except (ValueError, json.JSONDecodeError) as error:
            raise CredentialConfigurationError(Reason.PROXY_INVALID) from error
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in values.items()
        ):
            raise CredentialConfigurationError(Reason.PROXY_INVALID)
        return values
