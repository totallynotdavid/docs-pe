from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from browser.cli.direct import parse_args


if TYPE_CHECKING:
    from pathlib import Path


def test_parses_paths_and_derives_private_state(tmp_path: Path) -> None:
    source = tmp_path / "numbers.csv"
    source.touch()
    output = tmp_path / "carriers.csv"
    config = parse_args(
        ["--input", str(source), "--output", str(output), "--site", "portabilidad"]
    )
    assert config.site == "portabilidad"
    assert config.state_db == tmp_path / "carriers.state.sqlite3"
    assert config.control is None
    # Proxy is on by default so bulk runs never hammer a site from one IP.
    assert config.use_proxy is True


def test_no_proxy_flag_disables_the_proxy(tmp_path: Path) -> None:
    source = tmp_path / "numbers.csv"
    source.touch()
    config = parse_args(
        [
            "--input",
            str(source),
            "--output",
            str(tmp_path / "carriers.csv"),
            "--site",
            "portabilidad",
            "--no-proxy",
        ]
    )
    assert config.use_proxy is False


def test_rejects_control_not_served_by_site(tmp_path: Path) -> None:
    source = tmp_path / "numbers.csv"
    source.touch()
    with pytest.raises(SystemExit):
        # A DNI is not a phone, so portabilidad cannot use it as a control.
        parse_args(
            [
                "--input",
                str(source),
                "--output",
                str(tmp_path / "carriers.csv"),
                "--site",
                "portabilidad",
                "--control",
                "12345678",
            ]
        )


def test_rejects_unknown_site(tmp_path: Path) -> None:
    source = tmp_path / "numbers.csv"
    source.touch()
    with pytest.raises(SystemExit):
        parse_args(
            [
                "--input",
                str(source),
                "--output",
                str(tmp_path / "carriers.csv"),
                "--site",
                "nope",
            ]
        )
