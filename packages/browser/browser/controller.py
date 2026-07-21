from __future__ import annotations

from typing import Any, Protocol


class PageController(Protocol):
    """The seam between a browser backend and a site page.

    A backend (direct CDP over websocket) satisfies this; a site page consumes
    it. The site page never touches Chrome, and the backend never knows a site.
    """

    def evaluate(self, expression: str) -> Any: ...

    def clear_cookies(self) -> None: ...

    def open(self, url: str) -> None: ...

    def gui_click_element(self, selector: str) -> None: ...

    def gui_press_keys(self, keys: list[str]) -> None: ...
