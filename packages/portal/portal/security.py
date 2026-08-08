import hashlib
import hmac
import secrets

from urllib.parse import urlparse

import pyotp

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher


# Pinned rather than taken from PasswordHash.recommended(): those defaults can
# change with a pwdlib release, which would silently move every new hash to
# parameters nobody reviewed. These are OWASP's current Argon2id figures
# (m=19 MiB, t=2, p=1) and changing them is a deliberate, auditable edit.
# https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html
ARGON2_MEMORY_KIB = 19456
ARGON2_TIME_COST = 2
ARGON2_PARALLELISM = 1

TOTP_DIGITS = 6
TOTP_INTERVAL_SECONDS = 30

# One step either side, so a clock a few seconds out still authenticates.
TOTP_WINDOW_STEPS = 1

RECOVERY_CODE_COUNT = 10

_password_hash = PasswordHash(
    (
        Argon2Hasher(
            memory_cost=ARGON2_MEMORY_KIB,
            time_cost=ARGON2_TIME_COST,
            parallelism=ARGON2_PARALLELISM,
        ),
    )
)
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
    # utf-8 rather than ascii: every token this hashes is urlsafe base64, but a
    # bearer header is attacker-controlled and must not be able to raise here.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def valid_csrf(submitted: str | None, expected: str) -> bool:
    return bool(submitted and hmac.compare_digest(submitted, expected))


def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_matches(secret: str, code: str) -> bool:
    """Verify a TOTP code (RFC 6238, 30s step, 6 digits, one step of drift)."""
    digits = code.strip().replace(" ", "")

    if len(digits) != TOTP_DIGITS or not digits.isdigit():
        return False

    return _totp(secret).verify(digits, valid_window=TOTP_WINDOW_STEPS)


def totp_enrollment_uri(secret: str, *, email: str, issuer: str) -> str:
    return _totp(secret).provisioning_uri(name=email, issuer_name=issuer)


def new_recovery_codes() -> tuple[str, ...]:
    return tuple(secrets.token_urlsafe(16) for _ in range(RECOVERY_CODE_COUNT))


def new_worker_credential() -> str:
    return secrets.token_urlsafe(32)


def same_origin(
    *,
    origin: str | None,
    referer: str | None,
    trusted_origin: str,
    sec_fetch_site: str | None = None,
) -> bool:
    """Fetch Metadata decides alone when present: a browser sets Sec-Fetch-Site
    from the request's real initiator and scripts cannot override it, unlike
    Origin/Referer, which some legacy or privacy-mode clients omit or a
    misconfigured proxy can rewrite. Falling back to Origin/Referer on top of
    an already-trusted "same-origin" verdict would only add a way to fail
    open, not a way to fail more safely.
    https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
    """
    if sec_fetch_site:
        return sec_fetch_site == "same-origin"

    supplied = origin or referer

    if not supplied:
        return False

    actual = urlparse(supplied)
    expected = urlparse(trusted_origin)

    return (actual.scheme, actual.netloc) == (expected.scheme, expected.netloc)


def _totp(secret: str) -> pyotp.TOTP:
    return pyotp.TOTP(
        secret,
        digits=TOTP_DIGITS,
        interval=TOTP_INTERVAL_SECONDS,
    )
