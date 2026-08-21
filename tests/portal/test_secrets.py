from __future__ import annotations

import pytest

from portal.credentials.masterkey import MasterKeyring, new_master_key_line
from portal.credentials.secrets import EnvelopeProtector, decode_config, encode_config
from portal.domain.errors import CredentialConfigurationError, Reason
from portal.domain.models import ProtectedSecret

from tests.portal.conftest import MASTER_KEY, MASTER_KEY_VERSION


def test_a_configuration_survives_the_round_trip(
    protector: EnvelopeProtector,
) -> None:
    values = {"username": "equipo", "password": "clave", "country": "PE"}

    sealed = protector.protect(encode_config(values))

    assert sealed.master_key_version == MASTER_KEY_VERSION
    assert decode_config(protector.reveal(sealed)) == values


def test_every_payload_gets_its_own_data_key(protector: EnvelopeProtector) -> None:
    payload = encode_config({"password": "la-misma-clave"})

    first = protector.protect(payload)
    second = protector.protect(payload)

    # Identical input, unrelated stored bytes: a leaked database shows nothing
    # about which credentials match each other.
    assert first.wrapped_data_key != second.wrapped_data_key
    assert first.ciphertext != second.ciphertext


def test_the_payload_is_never_stored_in_the_clear(
    protector: EnvelopeProtector,
) -> None:
    sealed = protector.protect(encode_config({"password": "clave-secreta"}))

    assert b"clave-secreta" not in sealed.ciphertext
    assert b"clave-secreta" not in sealed.wrapped_data_key


def test_a_tampered_ciphertext_is_refused(protector: EnvelopeProtector) -> None:
    sealed = protector.protect(encode_config({"password": "clave"}))
    tampered = ProtectedSecret(
        ciphertext=sealed.ciphertext[:-1] + bytes([sealed.ciphertext[-1] ^ 1]),
        wrapped_data_key=sealed.wrapped_data_key,
        master_key_version=sealed.master_key_version,
    )

    with pytest.raises(CredentialConfigurationError) as refused:
        protector.reveal(tampered)

    assert refused.value.reason is Reason.SECRET_UNREADABLE


def test_a_truncated_ciphertext_is_refused(protector: EnvelopeProtector) -> None:
    with pytest.raises(CredentialConfigurationError):
        protector.reveal(ProtectedSecret(b"corto", b"", MASTER_KEY_VERSION))


def test_a_rotated_keyring_still_opens_what_the_old_key_wrapped() -> None:
    """The reason the envelope is worth its complexity.

    Rotation prepends a key. Payload ciphertext is untouched, so nothing has to
    be rewritten for secrets to keep opening and new ones to use the new key.
    """
    old = EnvelopeProtector(
        MasterKeyring.from_lines([f"{MASTER_KEY_VERSION} {MASTER_KEY}"])
    )
    sealed = old.protect(b"clave-del-proxy")

    rotated = EnvelopeProtector(
        MasterKeyring.from_lines(
            [new_master_key_line("v2"), f"{MASTER_KEY_VERSION} {MASTER_KEY}"]
        )
    )

    assert rotated.reveal(sealed) == b"clave-del-proxy"
    assert rotated.protect(b"nueva").master_key_version == "v2"


def test_dropping_a_key_still_in_use_fails_loudly() -> None:
    """An operator error, not a corrupt secret, so it must not read as one."""
    sealed = EnvelopeProtector(
        MasterKeyring.from_lines([f"{MASTER_KEY_VERSION} {MASTER_KEY}"])
    ).protect(b"clave-del-proxy")

    without_the_old_key = EnvelopeProtector(
        MasterKeyring.from_lines([new_master_key_line("v2")])
    )

    with pytest.raises(RuntimeError, match=MASTER_KEY_VERSION):
        without_the_old_key.reveal(sealed)


def test_a_keyring_needs_at_least_one_key() -> None:
    with pytest.raises(RuntimeError, match="no master keys"):
        MasterKeyring.from_lines(["# solo un comentario", ""])


@pytest.mark.parametrize(
    "line",
    ["v1", "v1 no-es-base64!", "v1 Y2M=", "v 1 clave"],
)
def test_a_malformed_key_line_names_its_line(line: str) -> None:
    with pytest.raises(RuntimeError, match="line 1"):
        MasterKeyring.from_lines([line])


@pytest.mark.parametrize(
    "payload",
    [b"no es json", b'["lista"]', b'{"lifetime_minutes": 10}'],
)
def test_only_a_flat_string_mapping_decodes_as_configuration(payload: bytes) -> None:
    with pytest.raises(CredentialConfigurationError) as refused:
        decode_config(payload)

    assert refused.value.reason is Reason.PROXY_INVALID
