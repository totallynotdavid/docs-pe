from __future__ import annotations

import base64
import hashlib
import json

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import cbor2
import pyotp
import pytest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from portal.domain.errors import ProvisioningError, Reason
from portal.domain.models import RequestTrace
from portal.security import hash_password

from tests.portal.conftest import (
    ORIGIN,
    PASSWORD,
    csrf_token,
    seed_site_admin,
    seed_user,
    sync_client,
)


if TYPE_CHECKING:
    from uuid import UUID

    import asyncpg

    from litestar import Litestar
    from portal.application.provisioning import ProvisioningService


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _secret_from_uri(enrollment_uri: str) -> str:
    return parse_qs(urlparse(enrollment_uri).query)["secret"][0]


class VirtualAuthenticator:
    """A minimal FIDO2 authenticator for the verification tests."""

    def __init__(self) -> None:
        self.credential_id = f"virtual-{id(self)}".encode()
        self._private_key = ec.generate_private_key(ec.SECP256R1())

    def register(self, options: dict[str, object]) -> dict[str, object]:
        numbers = self._private_key.public_key().public_numbers()
        cose_key = cbor2.dumps(
            {
                1: 2,  # kty: EC2
                3: -7,  # alg: ES256
                -1: 1,  # crv: P-256
                -2: numbers.x.to_bytes(32, "big"),
                -3: numbers.y.to_bytes(32, "big"),
            }
        )

        rp = options["rp"]
        assert isinstance(rp, dict)
        rp_id_hash = hashlib.sha256(str(rp["id"]).encode()).digest()

        attested = (
            b"\x00" * 16  # aaguid
            + len(self.credential_id).to_bytes(2, "big")
            + self.credential_id
            + cose_key
        )
        auth_data = rp_id_hash + bytes([0b01000101]) + (0).to_bytes(4, "big") + attested

        client_data = json.dumps(
            {
                "type": "webauthn.create",
                "challenge": options["challenge"],
                "origin": ORIGIN,
            }
        ).encode()

        attestation_object = cbor2.dumps(
            {"fmt": "none", "attStmt": {}, "authData": auth_data}
        )

        return {
            "id": _b64url(self.credential_id),
            "rawId": _b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64url(client_data),
                "attestationObject": _b64url(attestation_object),
                "transports": ["internal"],
            },
            "clientExtensionResults": {},
        }

    def authenticate(
        self,
        options: dict[str, object],
        *,
        sign_count: int = 1,
    ) -> dict[str, object]:
        rp_id_hash = hashlib.sha256(str(options["rpId"]).encode()).digest()
        auth_data = rp_id_hash + bytes([0b00000101]) + sign_count.to_bytes(4, "big")

        client_data = json.dumps(
            {
                "type": "webauthn.get",
                "challenge": options["challenge"],
                "origin": ORIGIN,
            }
        ).encode()

        signature = self._private_key.sign(
            auth_data + hashlib.sha256(client_data).digest(),
            ec.ECDSA(hashes.SHA256()),
        )

        return {
            "id": _b64url(self.credential_id),
            "rawId": _b64url(self.credential_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": _b64url(client_data),
                "authenticatorData": _b64url(auth_data),
                "signature": _b64url(signature),
                "userHandle": None,
            },
            "clientExtensionResults": {},
        }


async def _seed_password_user(pool: asyncpg.Pool, email: str) -> UUID:
    user_id = await seed_user(pool, email=email)

    await pool.execute(
        "UPDATE portal_users SET password_hash = $2 WHERE id = $1",
        user_id,
        hash_password(PASSWORD),
    )

    return user_id


async def test_totp_setup_is_confirm_gated(
    pool: asyncpg.Pool,
    provisioning: ProvisioningService,
) -> None:
    # The database trigger requires an active administrator for this write.
    await seed_site_admin(pool, "admin@osiptel.test")
    user_id = await seed_user(pool, email="candidata@osiptel.test")

    setup = await provisioning.begin_totp_setup(user_id)
    assert setup.enrollment_uri.startswith("otpauth://totp/")

    unenrolled = await pool.fetchval(
        "SELECT mfa_enabled FROM portal_users WHERE id = $1", user_id
    )
    assert unenrolled is False

    with pytest.raises(ProvisioningError) as excinfo:
        await provisioning.confirm_totp_setup(
            user_id,
            setup_token=setup.setup_token,
            code="000000",
        )
    assert excinfo.value.reason is Reason.TOTP_CODE_INVALID

    still_unenrolled = await pool.fetchval(
        "SELECT mfa_enabled FROM portal_users WHERE id = $1", user_id
    )
    assert still_unenrolled is False

    # A typo does not burn the setup_token: the same QR/secret still works.
    code = pyotp.TOTP(_secret_from_uri(setup.enrollment_uri)).now()
    recovery_codes = await provisioning.confirm_totp_setup(
        user_id,
        setup_token=setup.setup_token,
        code=code,
    )
    assert recovery_codes is not None
    assert len(recovery_codes) > 0

    enrolled = await pool.fetchval(
        "SELECT mfa_enabled FROM portal_users WHERE id = $1", user_id
    )
    assert enrolled is True

    # A spent setup_token cannot be replayed.
    with pytest.raises(ProvisioningError) as replay:
        await provisioning.confirm_totp_setup(
            user_id,
            setup_token=setup.setup_token,
            code=code,
        )
    assert replay.value.reason is Reason.SETUP_EXPIRED


async def test_passkey_registration_and_last_factor_removal_policy(
    pool: asyncpg.Pool,
    provisioning: ProvisioningService,
) -> None:
    admin_id = await seed_site_admin(pool, "admin@osiptel.test")
    candidate_id = await seed_user(pool, email="passkey-only@osiptel.test")
    authenticator = VirtualAuthenticator()

    setup = await provisioning.begin_passkey_registration(candidate_id)
    response = authenticator.register(json.loads(setup.options_json))

    recovery_codes = await provisioning.confirm_passkey_registration(
        candidate_id,
        setup_token=setup.setup_token,
        response_json=json.dumps(response),
        label="Llave de prueba",
    )
    assert recovery_codes is not None

    passkeys = await provisioning.passkeys(candidate_id)
    assert len(passkeys) == 1
    assert passkeys[0].label == "Llave de prueba"
    assert passkeys[0].credential_id == authenticator.credential_id

    # Promote the candidate: they already carry a factor, so no pending state.
    needs_setup = await provisioning.promote_to_site_admin(
        admin_id,
        user_id=candidate_id,
        mfa_verified_at=datetime.now(UTC),
        trace=RequestTrace(),
    )
    assert needs_setup is False

    with pytest.raises(ProvisioningError) as excinfo:
        await provisioning.remove_passkey(
            candidate_id,
            credential_id=passkeys[0].id,
        )
    assert excinfo.value.reason is Reason.LAST_SECOND_FACTOR

    # With a second factor added, removing the passkey is allowed again.
    totp_setup = await provisioning.begin_totp_setup(candidate_id)
    await provisioning.confirm_totp_setup(
        candidate_id,
        setup_token=totp_setup.setup_token,
        code=pyotp.TOTP(_secret_from_uri(totp_setup.enrollment_uri)).now(),
    )

    await provisioning.remove_passkey(candidate_id, credential_id=passkeys[0].id)
    assert await provisioning.passkeys(candidate_id) == ()


async def test_passkey_login_as_second_factor(
    pool: asyncpg.Pool,
    provisioning: ProvisioningService,
    app: Litestar,
) -> None:
    await seed_site_admin(pool, "admin@osiptel.test")
    user_id = await _seed_password_user(pool, "segundo-factor@osiptel.test")
    authenticator = VirtualAuthenticator()

    setup = await provisioning.begin_totp_setup(user_id)
    await provisioning.confirm_totp_setup(
        user_id,
        setup_token=setup.setup_token,
        code=pyotp.TOTP(_secret_from_uri(setup.enrollment_uri)).now(),
    )

    passkey_setup = await provisioning.begin_passkey_registration(user_id)
    await provisioning.confirm_passkey_registration(
        user_id,
        setup_token=passkey_setup.setup_token,
        response_json=json.dumps(
            authenticator.register(json.loads(passkey_setup.options_json))
        ),
        label="Llave",
    )

    with sync_client(app) as client:
        page = client.get("/login")
        password_response = client.post(
            "/login",
            data={
                "email": "segundo-factor@osiptel.test",
                "password": PASSWORD,
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        assert password_response.headers["location"] == "/login/mfa"

        options_response = client.post(
            "/login/passkey/options", headers={"Origin": ORIGIN}
        )
        assert options_response.status_code == 200
        payload = options_response.json()

        assertion = authenticator.authenticate(payload["options"], sign_count=1)
        verify_response = client.post(
            "/login/passkey/verify",
            data={
                "login_token": payload["loginToken"],
                "response": json.dumps(assertion),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert verify_response.headers["location"] == "/"
        assert "__Host-portal-id" in client.cookies or "portal-id" in client.cookies


async def test_discoverable_passkey_login_needs_no_password(
    pool: asyncpg.Pool,
    provisioning: ProvisioningService,
    app: Litestar,
) -> None:
    await _seed_password_user(pool, "passwordless@osiptel.test")
    user_id = await pool.fetchval(
        "SELECT id FROM portal_users WHERE email = 'passwordless@osiptel.test'"
    )
    authenticator = VirtualAuthenticator()

    setup = await provisioning.begin_passkey_registration(user_id)
    await provisioning.confirm_passkey_registration(
        user_id,
        setup_token=setup.setup_token,
        response_json=json.dumps(
            authenticator.register(json.loads(setup.options_json))
        ),
        label="Llave",
    )

    with sync_client(app) as client:
        # No password step at all: straight to the discoverable challenge.
        options_response = client.post(
            "/login/passkey/options", headers={"Origin": ORIGIN}
        )
        payload = options_response.json()
        assert payload["options"].get("allowCredentials") in (None, [])

        assertion = authenticator.authenticate(payload["options"], sign_count=1)
        verify_response = client.post(
            "/login/passkey/verify",
            data={
                "login_token": payload["loginToken"],
                "response": json.dumps(assertion),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )

        assert verify_response.headers["location"] == "/"

    # Reusing the sign_count identifies a replay or cloned authenticator.
    with sync_client(app) as client2:
        options_response_2 = client2.post(
            "/login/passkey/options", headers={"Origin": ORIGIN}
        )
        payload_2 = options_response_2.json()
        replayed = authenticator.authenticate(payload_2["options"], sign_count=1)
        replay_response = client2.post(
            "/login/passkey/verify",
            data={
                "login_token": payload_2["loginToken"],
                "response": json.dumps(replayed),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        assert replay_response.headers["location"] == "/login?error=1"


async def test_admin_promotion_completes_via_self_service_totp(
    pool: asyncpg.Pool,
    provisioning: ProvisioningService,
    app: Litestar,
) -> None:
    """End-to-end: ensure_site_admin leaves the account pending, and it
    reaches is_site_admin only once it completes its own enrollment, the same
    path a signed-in user reaches through /security/setup."""
    hashed = hash_password(PASSWORD)
    administrator, needs_setup = await provisioning.ensure_site_admin(
        "bootstrap-pending@osiptel.test",
        hashed,
    )
    assert needs_setup is True
    assert administrator.is_site_admin is False

    with sync_client(app) as client:
        page = client.get("/login")
        response = client.post(
            "/login",
            data={
                "email": "bootstrap-pending@osiptel.test",
                "password": PASSWORD,
                "csrf_token": csrf_token(page.text),
            },
            headers={"Origin": ORIGIN},
            follow_redirects=False,
        )
        # No factor exists yet, so there is nothing to challenge: straight to
        # a session, redirected to /security instead of the dashboard.
        assert response.headers["location"] == "/security"
