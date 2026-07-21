from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from browser.cli.direct import parse_args


if TYPE_CHECKING:
    from pathlib import Path


def test_parses_paths_and_derives_private_state_and_profile(tmp_path: Path) -> None:
    source = tmp_path / "clients.csv"
    source.touch()
    binary = tmp_path / "chrome"
    binary.touch()
    output = tmp_path / "debts.csv"
    config = parse_args(
        ["--input", str(source), "--output", str(output), "--binary", str(binary)]
    )
    assert config.site == "entel"
    assert config.state_db == tmp_path / "debts.state.sqlite3"
    # The direct runner always derives a persistent, per-site profile path.
    assert config.profile == tmp_path / ".debts.entel-chrome"


def test_rejects_invalid_control_ruc(tmp_path: Path) -> None:
    source = tmp_path / "clients.csv"
    source.touch()
    binary = tmp_path / "chrome"
    binary.touch()
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--input",
                str(source),
                "--output",
                str(tmp_path / "debts.csv"),
                "--binary",
                str(binary),
                "--control-ruc",
                "invalid",
            ]
        )


def test_rejects_unknown_site(tmp_path: Path) -> None:
    source = tmp_path / "clients.csv"
    source.touch()
    binary = tmp_path / "chrome"
    binary.touch()
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--input",
                str(source),
                "--output",
                str(tmp_path / "debts.csv"),
                "--binary",
                str(binary),
                "--site",
                "nope",
            ]
        )
