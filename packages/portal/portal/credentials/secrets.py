from __future__ import annotations

import base64
import json
import os
import secrets

from binascii import Error as BinasciiError
from dataclasses import dataclass
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from portal.domain.errors import CredentialConfigurationError


@dataclass(frozen=True)
class ProtectedSecret:
    """Opaque storage material; neither field is exposed by a browser read model."""

    ciphertext: bytes
    key_id: str


class SecretProtector(Protocol):
    """Port for a deployment-managed secret/KMS implementation."""

    async def protect(self, values: dict[str, str]) -> ProtectedSecret: ...


class UnavailableSecretProtector:
    """Safe default until a deployment injects a real secret-protection adapter."""

    async def protect(self, values: dict[str, str]) -> ProtectedSecret:
        del values
        msg = "la protección de credenciales aún no está configurada"
        raise CredentialConfigurationError(msg)


class DevelopmentAesGcmSecretProtector:
    """Environment-injected AES-GCM adapter, deliberately separate from a future KMS.

    The key is never persisted with the ciphertext, returned to a caller, or rendered.
    Production deployments must inject its key as a deployment secret; a dedicated
    secret-manager adapter can replace this implementation later.
    """

    key_id = "development-environment"

    def __init__(self, encoded_key: str) -> None:
        try:
            padding = "=" * (-len(encoded_key) % 4)
            key = base64.urlsafe_b64decode((encoded_key + padding).encode("ascii"))
        except (BinasciiError, UnicodeEncodeError, ValueError) as error:
            msg = "la clave local de protección no tiene un formato válido"
            raise CredentialConfigurationError(msg) from error
        if len(key) != 32:
            msg = "la clave local de protección debe tener 32 bytes"
            raise CredentialConfigurationError(msg)
        self._cipher = AESGCM(key)

    @classmethod
    def from_environment(cls) -> DevelopmentAesGcmSecretProtector | None:
        value = os.environ.get("PORTAL_SECRET_PROTECTION_KEY", "").strip()
        if not value:
            value = os.environ.get("PORTAL_SECRET_KEY", "").strip()
        return cls(value) if value else None

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
            msg = "la credencial protegida no se puede leer"
            raise CredentialConfigurationError(msg) from error
        if not isinstance(values, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in values.items()
        ):
            msg = "la credencial protegida no tiene un formato válido"
            raise CredentialConfigurationError(msg)
        return values
