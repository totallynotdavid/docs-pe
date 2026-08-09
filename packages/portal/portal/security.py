import hashlib
import hmac
import secrets

from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlparse

import pyotp
import segno
import webauthn

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from webauthn.helpers import (
    base64url_to_bytes,
    bytes_to_base64url,
    parse_authentication_credential_json,
    parse_registration_credential_json,
)
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)


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


def totp_qr_svg(uri: str) -> str:
    """Inline <svg>, not an <img> data: URI: nothing to re-decode client-side."""
    return segno.make(uri, error="m").svg_inline(scale=4, dark="#000", light="#fff")


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


# --- WebAuthn/passkeys -------------------------------------------------------
#
# Thin wrappers around the `webauthn` package: callers never import it
# directly, the same way pyotp stays behind the TOTP functions above.


@dataclass(frozen=True)
class WebAuthnChallenge:
    """Options JSON for the browser, and the raw challenge to persist."""

    options_json: str
    challenge: bytes


@dataclass(frozen=True)
class VerifiedPasskeyRegistration:
    credential_id: bytes
    public_key: bytes
    sign_count: int
    transports: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedPasskeyAuthentication:
    new_sign_count: int


def new_webauthn_registration_options(
    *,
    rp_id: str,
    rp_name: str,
    user_id: bytes,
    user_email: str,
    exclude_credential_ids: Iterable[bytes] = (),
) -> WebAuthnChallenge:
    options = webauthn.generate_registration_options(
        rp_id=rp_id,
        rp_name=rp_name,
        user_id=user_id,
        user_name=user_email,
        user_display_name=user_email,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=credential_id)
            for credential_id in exclude_credential_ids
        ],
    )
    return WebAuthnChallenge(webauthn.options_to_json(options), options.challenge)


def new_webauthn_authentication_options(
    *,
    rp_id: str,
    allow_credential_ids: Iterable[bytes] = (),
) -> WebAuthnChallenge:
    allowed = [
        PublicKeyCredentialDescriptor(id=credential_id)
        for credential_id in allow_credential_ids
    ]
    options = webauthn.generate_authentication_options(
        rp_id=rp_id,
        # None (not []) so a passwordless/discoverable login isn't scoped to
        # any one account's credentials.
        allow_credentials=allowed or None,
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return WebAuthnChallenge(webauthn.options_to_json(options), options.challenge)


def webauthn_challenge_text(challenge: bytes) -> str:
    """A WebAuthn challenge round-trips through a JSON-encoded pending token
    between issuing it and verifying the response, so it needs a text form."""
    return bytes_to_base64url(challenge)


def webauthn_challenge_bytes(text: str) -> bytes:
    return base64url_to_bytes(text)


def webauthn_credential_id(response_json: str) -> bytes | None:
    """The raw credential id from an authentication response, read before the
    owning row (and its public key) is known."""
    try:
        return parse_authentication_credential_json(response_json).raw_id
    except WebAuthnException:
        return None


def verify_webauthn_registration(
    *,
    response_json: str,
    expected_challenge: bytes,
    expected_origin: str,
    expected_rp_id: str,
) -> VerifiedPasskeyRegistration | None:
    try:
        transports = tuple(
            transport.value
            for transport in parse_registration_credential_json(
                response_json
            ).response.transports
            or ()
        )
        verification = webauthn.verify_registration_response(
            credential=response_json,
            expected_challenge=expected_challenge,
            expected_rp_id=expected_rp_id,
            expected_origin=expected_origin,
            require_user_verification=True,
        )
    except WebAuthnException:
        return None

    return VerifiedPasskeyRegistration(
        credential_id=verification.credential_id,
        public_key=verification.credential_public_key,
        sign_count=verification.sign_count,
        transports=transports,
    )


def verify_webauthn_authentication(
    *,
    response_json: str,
    expected_challenge: bytes,
    expected_origin: str,
    expected_rp_id: str,
    public_key: bytes,
    sign_count: int,
) -> VerifiedPasskeyAuthentication | None:
    try:
        verification = webauthn.verify_authentication_response(
            credential=response_json,
            expected_challenge=expected_challenge,
            expected_rp_id=expected_rp_id,
            expected_origin=expected_origin,
            credential_public_key=public_key,
            credential_current_sign_count=sign_count,
            require_user_verification=True,
        )
    except WebAuthnException:
        return None

    return VerifiedPasskeyAuthentication(new_sign_count=verification.new_sign_count)
