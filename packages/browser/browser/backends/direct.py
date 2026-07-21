from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request

from typing import TYPE_CHECKING, Any

import websocket

from browser.errors import BrowserError


if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType


class ChromeDevTools:
    """Raw CDP over a websocket. Satisfies the PageController protocol; knows no
    site."""

    def __init__(self, websocket_url: str) -> None:
        self._socket = websocket.create_connection(
            websocket_url,
            timeout=10,
            suppress_origin=True,
            http_no_proxy=["127.0.0.1", "localhost"],
        )
        self._next_id = 0
        self._call("Runtime.enable")
        self._call("Page.enable")
        self._call("Network.enable")

    def close(self) -> None:
        self._socket.close(timeout=1)

    def evaluate(self, expression: str) -> Any:
        response = self._call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": False,
            },
        )
        if "exceptionDetails" in response:
            details = response["exceptionDetails"]
            description = details.get("exception", {}).get("description")
            msg = description or details.get("text") or "JavaScript evaluation failed"
            raise BrowserError(msg)
        return response.get("result", {}).get("value")

    def clear_cookies(self) -> None:
        self._call("Network.clearBrowserCookies")

    def open(self, url: str) -> None:
        self._call("Page.navigate", {"url": url})

    def gui_click_element(self, selector: str) -> None:
        self._call("Page.bringToFront")
        coordinates = self.evaluate(
            "(() => { const element = document.querySelector("
            + json.dumps(selector)
            + "); if (!element) return null; const rect = element.getBoundingClientRect();"
            " const borderX = Math.max(0, (outerWidth - innerWidth) / 2);"
            " const browserTop = Math.max(0, outerHeight - innerHeight - borderX);"
            " return {x: Math.round(screenX + borderX + rect.left + rect.width / 2),"
            " y: Math.round(screenY + browserTop + rect.top + rect.height / 2),"
            " width: rect.width, height: rect.height}; })()"
        )
        if not isinstance(coordinates, dict):
            msg = f"element is missing for GUI click: {selector}"
            raise BrowserError(msg)
        if coordinates.get("width", 0) <= 0 or coordinates.get("height", 0) <= 0:
            msg = f"element is not visible for GUI click: {selector}"
            raise BrowserError(msg)
        pyautogui = _pyautogui()
        pyautogui.click(int(coordinates["x"]), int(coordinates["y"]))

    def gui_press_keys(self, keys: list[str]) -> None:
        _pyautogui().write("".join(keys), interval=0.06)

    def _call(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._socket.send(
            json.dumps(
                {"id": request_id, "method": method, "params": params or {}},
                separators=(",", ":"),
            )
        )
        while True:
            message = json.loads(self._socket.recv())
            if message.get("id") != request_id:
                continue
            if "error" in message:
                error = message["error"]
                msg = f"Chrome DevTools {method} failed: {error.get('message', error)}"
                raise BrowserError(msg)
            return message.get("result", {})


class DirectBrowser:
    """Launch Google Chrome with a remote debugging port, wait for the site page,
    and yield a ChromeDevTools controller. Tears down the whole process group on
    exit. Site-agnostic: it only needs the URL to open and wait for."""

    def __init__(
        self,
        *,
        binary: Path,
        profile: Path,
        software_webgl: bool,
        url: str,
    ) -> None:
        self._binary = binary
        self._profile = profile
        self._software_webgl = software_webgl
        self._url = url
        self._process: subprocess.Popen[bytes] | None = None
        self._controller: ChromeDevTools | None = None

    def __enter__(self) -> ChromeDevTools:
        self._profile.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        command = [
            str(self._binary),
            f"--user-data-dir={self._profile}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--password-store=basic",
            "--window-position=0,0",
            "--window-size=1920,1080",
        ]
        if self._software_webgl:
            command.extend(["--enable-webgl", "--use-angle=swiftshader"])
        command.append(self._url)
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            websocket_url = _wait_for_page(
                port=port, process=self._process, url=self._url
            )
            self._controller = ChromeDevTools(websocket_url)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self._controller

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._controller is not None:
            self._controller.close()
        self._controller = None
        if self._process is not None and self._process.poll() is None:
            os.killpg(self._process.pid, signal.SIGTERM)
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self._process.pid, signal.SIGKILL)
                self._process.wait(timeout=5)
        self._process = None


def _wait_for_page(
    *, port: int, process: subprocess.Popen[bytes], url: str, timeout_s: float = 20.0
) -> str:
    endpoint = f"http://127.0.0.1:{port}/json/list"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            msg = f"Google Chrome exited during startup with code {process.returncode}"
            raise BrowserError(msg)
        try:
            with urllib.request.urlopen(endpoint, timeout=1) as response:
                targets = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            time.sleep(0.1)
            continue
        pages = [target for target in targets if target.get("type") == "page"]
        target = next(
            (page for page in pages if str(page.get("url", "")).startswith(url)),
            pages[0] if pages else None,
        )
        if target and target.get("webSocketDebuggerUrl"):
            return str(target["webSocketDebuggerUrl"])
        time.sleep(0.1)
    msg = "Google Chrome did not expose the site page through DevTools"
    raise BrowserError(msg)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pyautogui() -> Any:
    import pyautogui  # type: ignore[import-untyped]

    return pyautogui
