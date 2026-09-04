from __future__ import annotations

import pytest

from portal.admin.cli import main


@pytest.mark.parametrize("command", ["migrate", "bootstrap"])
def test_no_argument_commands_have_help(
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main([command, "--help"])

    assert error.value.code == 0
    output = capsys.readouterr().out
    assert output.startswith(f"usage: portal-admin {command} ")
