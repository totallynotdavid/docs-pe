from portal.security import totp_matches


def test_a_totp_secret_pyotp_rejects_counts_as_a_wrong_code() -> None:
    # Secrets issued under pyotp < 2.12 are 16 base32 characters, under the
    # 112-bit minimum pyotp now enforces.
    assert totp_matches("JBSWY3DPEHPK3PXP", "123456") is False
