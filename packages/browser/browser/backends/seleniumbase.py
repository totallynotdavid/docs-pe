from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from seleniumbase import sb_cdp  # type: ignore[import-untyped]

from browser.errors import BrowserError
from browser.session import SeleniumBaseSession

if TYPE_CHECKING:
    from types import TracebackType


# Use software WebGL for a stable fingerprint on headless Xvfb.
_SOFTWARE_WEBGL_ARGS = ["--use-angle=swiftshader", "--enable-webgl"]


class SeleniumBaseBrowser:
    def __init__(
        self,
        *,
        url: str,
        software_webgl: bool,
        proxy: str | None = None,
    ) -> None:
        self._url = url
        self._software_webgl = software_webgl
        self._proxy = proxy
        self._driver: object | None = None

    def __enter__(self) -> SeleniumBaseSession:
        if shutil.which("Xvfb") is None:
            msg = "Xvfb is required for the browser collector but is not installed"
            raise BrowserError(msg)

        browser_args = list(_SOFTWARE_WEBGL_ARGS) if self._software_webgl else []

        if self._proxy is not None:
            # Avoid SeleniumBase proxy interception; use the local relay directly.
            browser_args.append(f"--proxy-server={self._proxy}")

        self._driver = sb_cdp.Chrome(
            self._url,
            browser_args=browser_args or None,
        )

        return SeleniumBaseSession(self._driver)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._driver is not None:
            self._driver.quit()  # type: ignore[attr-defined]
            self._driver = None
