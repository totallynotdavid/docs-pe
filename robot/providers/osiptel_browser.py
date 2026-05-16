from __future__ import annotations

import logging
import random
import time

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from selenium.common.exceptions import WebDriverException
from seleniumbase import SB  # type: ignore[import-untyped]

from robot.domain.errors import (
    BanSignalError,
    CaptchaError,
    CaptchaTimeoutError,
    TransientTransportError,
)
from robot.obs.events import SESSION_OPEN
from robot.obs.logging import kv, new_session_id


if TYPE_CHECKING:
    from robot.providers.geonode import ProxySessionConfig


logger = logging.getLogger(__name__)

HOME_URL = "https://checatuslineas.osiptel.gob.pe/"
_STATE_EXPR = """(() => ({
  ready: document.readyState || '',
  href: location.href || '',
  title: document.title || '',
  scripts: document.scripts ? document.scripts.length : -1,
  gc: typeof window.grecaptcha,
  key: (document.querySelector('#hiddenRecaptchaKey')||{}).value || ''
}))()"""

_TOKEN_START_EXPR = """(() => {
  window.__rcTok = '';
  window.__rcErr = '';
  const key = (document.querySelector('#hiddenRecaptchaKey') || {}).value || '';
  const action = (document.querySelector('#hiddenAction') || {}).value || '';
  if (!window.grecaptcha || !key) {
    window.__rcErr = 'missing grecaptcha or key';
    return false;
  }
  window.grecaptcha.ready(function() {
    window.grecaptcha.execute(key, {action: action})
      .then(tok => window.__rcTok = tok || '')
      .catch(err => window.__rcErr = String(err));
  });
  return true;
})()"""


@dataclass(frozen=True)
class BrowserSessionSettings:
    chrome_binary: str = ""
    script_timeout_s: float = 45.0
    token_timeout_s: float = 20.0
    first_token_timeout_s: float = 40.0
    same_session_retries: int = 1
    first_token_jitter_max_s: float = 5.0


class BrowserSession:
    def __init__(
        self, *, proxy: ProxySessionConfig, settings: BrowserSessionSettings
    ) -> None:
        self._proxy = proxy
        self._settings = settings
        self._sb_cm: SB | None = None
        self._sb: SB | None = None
        self.session_id = new_session_id()
        self._tokens_generated = 0

    @property
    def proxy_id(self) -> str:
        return self._proxy.proxy_id

    @property
    def proxy_config(self) -> ProxySessionConfig:
        return self._proxy

    def open(self) -> None:
        kwargs: dict[str, Any] = {
            "uc": True,
            "headed": True,
            "xvfb": True,
            "proxy": self._proxy.as_selenium_proxy(),
        }
        if self._settings.chrome_binary:
            kwargs["binary_location"] = self._settings.chrome_binary

        started = time.perf_counter()
        try:
            self._sb_cm = SB(**kwargs)
            self._sb = self._sb_cm.__enter__()
            self._sb.driver.set_script_timeout(self._settings.script_timeout_s)
            self._sb.activate_cdp_mode(HOME_URL)
            logger.info(
                "%s %s",
                SESSION_OPEN,
                kv(
                    session_id=self.session_id,
                    proxy_id=self.proxy_id,
                    elapsed_ms=int((time.perf_counter() - started) * 1000),
                ),
            )
            ready_started = time.perf_counter()
            self.wait_ready()
            logger.info(
                "session_wait_ready %s",
                kv(
                    session_id=self.session_id,
                    elapsed_ms=int((time.perf_counter() - ready_started) * 1000),
                ),
            )
        except WebDriverException as exc:
            self.close()
            msg = f"failed to open browser session: {type(exc).__name__}: {exc}"
            raise TransientTransportError(msg) from exc
        except Exception as exc:
            self.close()
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            msg = f"failed to open browser session: {type(exc).__name__}: {exc}"
            raise TransientTransportError(msg) from exc

    def close(self) -> None:
        if self._sb_cm is None:
            return
        try:
            self._sb_cm.__exit__(None, None, None)
        finally:
            self._sb_cm = None
            self._sb = None

    def user_agent(self) -> str:
        sb = self._require_sb()
        value = sb.execute_script("(() => navigator.userAgent || '')()") or ""
        if isinstance(value, str) and value:
            return value
        return "Mozilla/5.0"

    def cookie_header(self) -> str:
        sb = self._require_sb()
        cookies = sb.cdp.get_all_cookies() or []
        chunks: list[str] = []
        for cookie in cookies:
            name = getattr(cookie, "name", None)
            value = getattr(cookie, "value", None)
            if isinstance(name, str) and isinstance(value, str) and name:
                chunks.append(f"{name}={value}")
        return "; ".join(chunks)

    def generate_token(self, *, poll_s: float = 0.25) -> str:
        is_first_token = self._tokens_generated == 0
        timeout_s = (
            self._settings.first_token_timeout_s
            if is_first_token
            else self._settings.token_timeout_s
        )
        if is_first_token and self._settings.first_token_jitter_max_s > 0:
            time.sleep(random.uniform(0.0, self._settings.first_token_jitter_max_s))

        attempts = self._settings.same_session_retries + 1
        last_exc: CaptchaError | None = None
        for _ in range(attempts):
            try:
                token = self._generate_token_once(timeout_s=timeout_s, poll_s=poll_s)
                self._tokens_generated += 1
                return token
            except CaptchaTimeoutError as exc:
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        msg = "captcha token generation failed unexpectedly"
        raise CaptchaError(msg)

    def _generate_token_once(self, *, timeout_s: float, poll_s: float) -> str:
        sb = self._require_sb()
        try:
            started = bool(sb.execute_script(_TOKEN_START_EXPR))
        except WebDriverException as exc:
            msg = f"captcha token script start failed: {type(exc).__name__}: {exc}"
            raise CaptchaError(msg) from exc
        if not started:
            msg = "failed to start recaptcha token generation"
            raise CaptchaError(msg)

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                token = sb.execute_script("(() => window.__rcTok || '')()") or ""
                token_err = sb.execute_script("(() => window.__rcErr || '')()") or ""
            except WebDriverException as exc:
                msg = f"captcha token polling failed: {type(exc).__name__}: {exc}"
                raise CaptchaError(msg) from exc
            if isinstance(token, str) and token.strip():
                return token.strip()
            if token_err:
                msg = f"captcha token generation failed: {token_err}"
                raise CaptchaError(msg)
            time.sleep(poll_s)

        msg = "captcha token not generated in time"
        raise CaptchaTimeoutError(msg)

    def wait_ready(self, *, timeout_s: float = 25.0, poll_s: float = 0.25) -> None:
        sb = self._require_sb()
        deadline = time.monotonic() + timeout_s
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                state = sb.execute_script(_STATE_EXPR) or {}
            except WebDriverException as exc:
                msg = (
                    f"osiptel page readiness probe failed: {type(exc).__name__}: {exc}"
                )
                raise TransientTransportError(msg) from exc
            if isinstance(state, dict):
                last_state = state
            if (
                state.get("scripts", 0) >= 20
                and state.get("gc") == "object"
                and state.get("key")
            ):
                return
            time.sleep(poll_s)

        msg = (
            "osiptel page not ready "
            f"ready={last_state.get('ready', '')} "
            f"href={last_state.get('href', '')} "
            f"title={last_state.get('title', '')} "
            f"scripts={last_state.get('scripts', '')} "
            f"gc={last_state.get('gc', '')} "
            f"has_key={bool(last_state.get('key'))}"
        )
        title = str(last_state.get("title", "")).lower()
        if "requested has been blocked" in title:
            raise BanSignalError(msg)
        raise TransientTransportError(msg)

    def _require_sb(self) -> SB:
        if self._sb is None:
            msg = "browser session not open"
            raise TransientTransportError(msg)
        return self._sb
