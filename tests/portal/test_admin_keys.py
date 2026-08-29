from __future__ import annotations

import stat

import pytest

from portal.admin.cli import main
from portal.credentials.masterkey import MasterKeyring, new_master_key_line


def test_key_install_creates_a_private_keyring(tmp_path) -> None:
    path = tmp_path / "master.key"

    main(["key", "install", "--path", str(path), "--version", "v1"])

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert MasterKeyring.from_file(path).active_version == "v1"


def test_key_install_does_not_overwrite_an_existing_file(tmp_path) -> None:
    path = tmp_path / "master.key"
    path.write_text(new_master_key_line("v1"), encoding="utf-8")

    with pytest.raises(SystemExit, match="refusing to overwrite"):
        main(["key", "install", "--path", str(path), "--version", "v2"])


def test_key_rotate_prepends_a_new_key_and_restricts_permissions(tmp_path) -> None:
    path = tmp_path / "master.key"
    path.write_text(new_master_key_line("v1") + "\n", encoding="utf-8")
    path.chmod(0o644)

    main(["key", "rotate", "--path", str(path), "--version", "v2"])

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert MasterKeyring.from_file(path).active_version == "v2"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
