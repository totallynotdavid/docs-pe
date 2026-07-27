from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.fernet import Fernet


_PBKDF2_ROUNDS = 600_000


def hash_password(password: str) -> str:
    if len(password) < 12:
        msg = "password must be at least 12 characters"
        raise ValueError(msg)
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(derived).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode(),
            base64.urlsafe_b64decode(salt.encode()),
            int(rounds),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, base64.urlsafe_b64decode(expected.encode()))


class SecretCipher:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode("utf-8"))

    def decrypt(self, value: bytes) -> str:
        return self._fernet.decrypt(value).decode("utf-8")


def fingerprint(value: str, secret: str) -> str:
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


def digest_submission(*, input_sha256: str, sources: list[str], provider: str) -> str:
    material = "\n".join([input_sha256, provider, *sorted(sources)])
    return hashlib.sha256(material.encode()).hexdigest()
