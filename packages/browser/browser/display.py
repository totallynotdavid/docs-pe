from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time

from pathlib import Path
from typing import TYPE_CHECKING, Self


if TYPE_CHECKING:
    from types import TracebackType


class DedicatedDisplay:
    """A private X display so GUI input cannot land in another browser."""

    def __init__(self, number: int | None = None) -> None:
        self._requested_number = number
        self._number = 0
        self._process: subprocess.Popen[bytes] | None = None
        self._authority: Path | None = None
        self._old_display: str | None = None
        self._old_authority: str | None = None

    def __enter__(self) -> Self:
        executable = shutil.which("Xvfb")
        if executable is None:
            msg = "Xvfb is required for the browser collector but is not installed"
            raise RuntimeError(msg)
        self._number = self._requested_number or _free_display_number()
        authority = tempfile.NamedTemporaryFile(prefix="browser-xauth-", delete=False)
        authority.close()
        self._authority = Path(authority.name)
        self._old_display = os.environ.get("DISPLAY")
        self._old_authority = os.environ.get("XAUTHORITY")
        self._process = subprocess.Popen(
            [
                executable,
                f":{self._number}",
                "-screen",
                "0",
                "1920x1080x24",
                "-listen",
                "tcp",
                "-nolisten",
                "unix",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_for_display(self._process, self._number)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        os.environ["DISPLAY"] = f"127.0.0.1:{self._number}"
        os.environ["XAUTHORITY"] = str(self._authority)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3)
            self._process = None
        if self._authority is not None:
            self._authority.unlink(missing_ok=True)
            self._authority = None
        _restore_env("DISPLAY", self._old_display)
        _restore_env("XAUTHORITY", self._old_authority)


def _free_display_number() -> int:
    for number in range(90, 200):
        if Path(f"/tmp/.X{number}-lock").exists():
            continue
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", 6000 + number)) != 0:
                return number
    msg = "no free X display number was found"
    raise RuntimeError(msg)


def _wait_for_display(process: subprocess.Popen[bytes], number: int) -> None:
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if process.poll() is not None:
            msg = f"Xvfb :{number} exited during startup"
            raise RuntimeError(msg)
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", 6000 + number)) == 0:
                return
        time.sleep(0.05)
    msg = f"Xvfb :{number} did not become ready"
    raise RuntimeError(msg)


def _restore_env(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
