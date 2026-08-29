from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from cli import preflight as preflight_mod
from core.domain.errors import ProxyConfigurationError


if TYPE_CHECKING:
    import pytest


def test_parse_requires_a_known_provider() -> None:
    args = preflight_mod.parse_args(["--provider", "geonode"])
    assert args.provider == "geonode"


async def test_run_reports_provider_and_exit_ip(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, object] = {}

    def values_from_environment(spec: object) -> dict[str, str]:
        seen["spec"] = spec
        return {"username": "user", "password": "secret"}

    preflight = AsyncMock(return_value="203.0.113.10")

    monkeypatch.setattr(
        preflight_mod,
        "values_from_environment",
        values_from_environment,
    )
    monkeypatch.setattr(preflight_mod, "preflight", preflight)

    assert await preflight_mod.run("geonode") == 0
    preflight.assert_awaited_once_with(
        "geonode", {"username": "user", "password": "secret"}
    )
    assert capsys.readouterr().out == "geonode: exit IP 203.0.113.10\n"


async def test_run_reports_a_safe_provider_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    message = "proxy credentials are invalid"
    preflight = AsyncMock(side_effect=ProxyConfigurationError(message))

    monkeypatch.setattr(
        preflight_mod,
        "values_from_environment",
        lambda _spec: {"username": "user", "password": "secret"},
    )
    monkeypatch.setattr(preflight_mod, "preflight", preflight)

    assert await preflight_mod.run("geonode") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "geonode: preflight failed: proxy credentials are invalid\n"
