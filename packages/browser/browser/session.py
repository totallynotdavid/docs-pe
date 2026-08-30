from __future__ import annotations

from typing import Any, Protocol


class Session(Protocol):
    def goto(self, url: str) -> None: ...

    def sleep(self, seconds: float) -> None: ...

    def type(self, selector: str, text: str) -> None: ...

    def press_keys(self, selector: str, text: str) -> None: ...

    def gui_write(self, text: str) -> None: ...

    def click(self, selector: str) -> None: ...

    def click_if_visible(self, selector: str) -> None: ...

    def gui_click_element(self, selector: str) -> None: ...

    def gui_click_captcha(self) -> None: ...

    def evaluate(self, expression: str) -> Any: ...

    def get_page_source(self) -> str: ...

    def get_text(self, selector: str = "body") -> str: ...

    def is_element_present(self, selector: str) -> bool: ...

    def wait_for_text(self, text: str, *, timeout: float) -> None: ...

    def clear_cookies(self) -> None: ...

    def save_screenshot(self, name: str, *, folder: str) -> None: ...


class SeleniumBaseSession:
    def __init__(self, driver: Any) -> None:
        self._driver = driver

    def goto(self, url: str) -> None:
        self._driver.goto(url)

    def sleep(self, seconds: float) -> None:
        self._driver.sleep(seconds)

    def type(self, selector: str, text: str) -> None:
        self._driver.type(selector, text)

    def press_keys(self, selector: str, text: str) -> None:
        self._driver.press_keys(selector, text)

    def gui_write(self, text: str) -> None:
        self._driver.gui_write(text)

    def click(self, selector: str) -> None:
        self._driver.click(selector)

    def click_if_visible(self, selector: str) -> None:
        self._driver.click_if_visible(selector)

    def gui_click_element(self, selector: str) -> None:
        self._driver.gui_click_element(selector)

    def gui_click_captcha(self) -> None:
        self._driver.gui_click_captcha()

    def evaluate(self, expression: str) -> Any:
        return self._driver.evaluate(expression)

    def get_page_source(self) -> str:
        return str(self._driver.get_page_source())

    def get_text(self, selector: str = "body") -> str:
        return str(self._driver.get_text(selector))

    def is_element_present(self, selector: str) -> bool:
        return bool(self._driver.is_element_present(selector))

    def wait_for_text(self, text: str, *, timeout: float) -> None:
        self._driver.wait_for_text(text, timeout=timeout)

    def clear_cookies(self) -> None:
        self._driver.clear_cookies()

    def save_screenshot(self, name: str, *, folder: str) -> None:
        self._driver.save_screenshot(name, folder=folder)
