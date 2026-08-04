import hashlib
import hmac
import secrets

from urllib.parse import urlparse

from pwdlib import PasswordHash


# Uses Argon2id when the recommended extra is installed.
_password_hash = PasswordHash.recommended()
_DUMMY_HASH = _password_hash.hash("portal-not-a-real-password")


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    return _password_hash.verify(password, encoded)


def verify_dummy_password(password: str) -> None:
    """Keep unknown-account work comparable to a failed known-account login."""
    _password_hash.verify(password, _DUMMY_HASH)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def valid_csrf(submitted: str | None, expected: str) -> bool:
    return bool(submitted and hmac.compare_digest(submitted, expected))


def same_origin(
    *,
    origin: str | None,
    referer: str | None,
    trusted_origin: str,
) -> bool:
    supplied = origin or referer

    if not supplied:
        return False

    actual = urlparse(supplied)
    expected = urlparse(trusted_origin)

    return (actual.scheme, actual.netloc) == (expected.scheme, expected.netloc)
