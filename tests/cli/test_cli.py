from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from cli.main import parse_args


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


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"--input": "does-not-exist.csv"}, "--input file not found"),
        ({"--sites": "nope"}, "unknown site"),
        ({"--session-budget": "0"}, "--session-budget must be >= 1"),
        ({"--ban-cooldown-s": "-1"}, "--ban-cooldown-s must be >= 0"),
        (
            {"--wait-min-s": "5", "--wait-max-s": "1"},
            "--wait-max-s must be >= --wait-min-s",
        ),
    ],
)
def test_an_invalid_invocation_is_rejected(
    tmp_path: Path,
    overrides: dict[str, str],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        parse_args(_args(tmp_path, **overrides))
    assert message in capsys.readouterr().err
