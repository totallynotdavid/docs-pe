from __future__ import annotations

import json
import secrets

from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from portal.credentials.masterkey import NONCE_BYTES
from portal.domain.errors import CredentialConfigurationError, Reason
from portal.domain.models import ProtectedSecret


if TYPE_CHECKING:
    from collections.abc import Mapping

    from portal.credentials.masterkey import MasterKeyring


class EnvelopeProtector:
    """Envelope encryption: one fresh data key per payload, wrapped by a master key.

    A payload is encrypted under a key that exists only for the duration of the
    call, and only the wrapped form of that key is stored beside the ciphertext.

    Rotating a master key re-wraps the stored data keys and leaves payload
    ciphertext alone, so rotation is a pass over 60-byte blobs rather than a
    re-encryption of every credential. Without the envelope, rotation would mean
    decrypting and rewriting every secret in the database.
    """

    def __init__(self, keyring: MasterKeyring) -> None:
        self._keyring = keyring

    def protect(self, payload: bytes) -> ProtectedSecret:
        data_key = self._keyring.generate_data_key()
        nonce = secrets.token_bytes(NONCE_BYTES)

        # The plaintext key stays a local, and nothing here returns or logs it.
        # CPython cannot be made to scrub the buffer, so the guarantee this
        # layer actually offers is the narrow lifetime, not erasure.
        ciphertext = AESGCM(data_key.plaintext).encrypt(nonce, payload, None)

        return ProtectedSecret(
            ciphertext=nonce + ciphertext,
            wrapped_data_key=data_key.wrapped,
            master_key_version=data_key.key_version,
        )

    def reveal(self, secret: ProtectedSecret) -> bytes:
        if len(secret.ciphertext) <= NONCE_BYTES:
            raise CredentialConfigurationError(Reason.SECRET_UNREADABLE)

        try:
            plaintext_key = self._keyring.unwrap(
                secret.wrapped_data_key,
                secret.master_key_version,
            )

            return AESGCM(plaintext_key).decrypt(
                secret.ciphertext[:NONCE_BYTES],
                secret.ciphertext[NONCE_BYTES:],
                None,
            )
        except (InvalidTag, ValueError) as error:
            raise CredentialConfigurationError(Reason.SECRET_UNREADABLE) from error


def encode_config(values: Mapping[str, str]) -> bytes:
    return json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")


def decode_config(payload: bytes) -> dict[str, str]:
    try:
        values = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as error:
        raise CredentialConfigurationError(Reason.PROXY_INVALID) from error

    if not isinstance(values, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in values.items()
    ):
        raise CredentialConfigurationError(Reason.PROXY_INVALID)

    return values
