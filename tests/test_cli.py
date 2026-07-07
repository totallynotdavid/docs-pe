from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from robot.cli import parse_args


if TYPE_CHECKING:
    from pathlib import Path


def _args(tmp_path: Path, **overrides: str) -> list[str]:
    input_csv = tmp_path / "in.csv"
    input_csv.touch()
    args = {
        "--input": str(input_csv),
        "--output": str(tmp_path / "out.csv"),
        "--sites": "sunat",
    }
    args.update(overrides)
    flat: list[str] = []
    for key, value in args.items():
        flat.extend([key, value])
    return flat


def test_parses_a_minimal_valid_invocation(tmp_path: Path) -> None:
    cfg = parse_args(_args(tmp_path))
    assert [site.name for site in cfg.sites] == ["sunat"]
    assert cfg.dedupe is True
    assert cfg.session_budget is None


def test_sites_are_lowercased_split_and_ordered(tmp_path: Path) -> None:
    cfg = parse_args(_args(tmp_path, **{"--sites": "SUNAT, osiptel"}))
    assert [site.name for site in cfg.sites] == ["sunat", "osiptel"]


def test_a_missing_input_file_is_rejected(tmp_path: Path) -> None:
    args = _args(tmp_path)
    args[args.index("--input") + 1] = str(tmp_path / "does-not-exist.csv")
    with pytest.raises(SystemExit):
        parse_args(args)


def test_an_unknown_site_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        parse_args(_args(tmp_path, **{"--sites": "nope"}))


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--session-budget", "0"),
        ("--workers", "0"),
        ("--ban-cooldown-s", "-1"),
    ],
)
def test_out_of_range_overrides_are_rejected(
    tmp_path: Path, flag: str, value: str
) -> None:
    with pytest.raises(SystemExit):
        parse_args(_args(tmp_path, **{flag: value}))


def test_wait_max_below_wait_min_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        parse_args(_args(tmp_path, **{"--wait-min-s": "5", "--wait-max-s": "1"}))
