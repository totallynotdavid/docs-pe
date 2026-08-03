from __future__ import annotations

import shutil

from typing import TYPE_CHECKING

from seleniumbase import sb_cdp  # type: ignore[import-untyped]

from browser.errors import BrowserError
from browser.session import SeleniumBaseSession


if TYPE_CHECKING:
    from types import TracebackType


# SwiftShader gives a consistent software WebGL fingerprint on the headless Xvfb
# display, where no GPU is present.
_SOFTWARE_WEBGL_ARGS = ["--use-angle=swiftshader", "--enable-webgl"]


class SeleniumBaseBrowser:
    """Launch a SeleniumBase Pure CDP browser and yield a Session.

    SeleniumBase starts its own private Xvfb display per process, so concurrent
    runs never share a GUI-input target.
    """

    def __init__(
        self, *, url: str, software_webgl: bool, proxy: str | None = None
    ) -> None:
        self._url = url
        self._software_webgl = software_webgl
        self._proxy = proxy
        self._driver: object | None = None

    def __enter__(self) -> SeleniumBaseSession:
        if shutil.which("Xvfb") is None:
            msg = "Xvfb is required for the browser collector but is not installed"
            raise BrowserError(msg)
        args = list(_SOFTWARE_WEBGL_ARGS) if self._software_webgl else []
        if self._proxy is not None:
            # A Chrome flag, because SeleniumBase's own proxy= argument enables CDP
            # Fetch interception and stalls heavy pages. This proxy is the local relay.
            args.append(f"--proxy-server={self._proxy}")
        self._driver = sb_cdp.Chrome(self._url, browser_args=args or None)
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
