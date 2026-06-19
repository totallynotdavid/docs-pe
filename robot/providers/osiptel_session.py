from __future__ import annotations

import logging
import time

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx

from robot.domain.errors import BanSignalError, ParseError, TransientTransportError
from robot.obs.events import (
    FETCH_PAGE_OK,
    FETCH_PAGE_START,
    OSIPTEL_REQUEST_FAILED,
    SESSION_OPEN,
)
from robot.obs.logging import kv, new_session_id


if TYPE_CHECKING:
    from robot.providers.geonode import ProxySessionConfig


logger = logging.getLogger(__name__)

HOME_URL = "https://checatuslineas.osiptel.gob.pe/"
API_URL = "https://checatuslineas.osiptel.gob.pe/Consultas/GetAllCabeceraConsulta/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class OsiptelSessionSettings:
    request_timeout_s: float = 45.0


class OsiptelSession:
    def __init__(
        self, *, proxy: ProxySessionConfig, settings: OsiptelSessionSettings
    ) -> None:
        self._proxy = proxy
        self._settings = settings
        self._client: httpx.Client | None = None
        self._cookie_header = ""
        self.session_id = new_session_id()

    @property
    def proxy_id(self) -> str:
        return self._proxy.proxy_id

    @property
    def proxy_config(self) -> ProxySessionConfig:
        return self._proxy

    def open(self) -> None:
        started = time.perf_counter()
        self._client = httpx.Client(
            proxy=self._proxy.as_http_proxy_url(),
            timeout=self._settings.request_timeout_s,
            headers={"User-Agent": DEFAULT_USER_AGENT},
            follow_redirects=True,
        )
        try:
            self.wait_ready()
        except Exception:
            self.close()
            raise

        logger.info(
            "%s %s",
            SESSION_OPEN,
            kv(
                session_id=self.session_id,
                proxy_id=self.proxy_id,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            ),
        )

    def close(self) -> None:
        if self._client is None:
            return
        self._client.close()
        self._client = None

    def user_agent(self) -> str:
        return DEFAULT_USER_AGENT

    def cookie_header(self) -> str:
        return self._cookie_header

    def fetch_json(
        self,
        data: dict[str, str],
        *,
        ruc: str,
        draw: int,
        start: int,
        length: int,
    ) -> Any:
        client = self._require_client()
        started = time.perf_counter()
        logger.info(
            "%s %s",
            FETCH_PAGE_START,
            kv(ruc=ruc, draw=draw, start=start, length=length),
        )
        try:
            response = client.post(API_URL, data=data, headers=self._api_headers())
        except httpx.HTTPError as exc:
            msg = f"osiptel request transport failed: {type(exc).__name__}: {exc}"
            raise TransientTransportError(msg) from exc

        status = response.status_code
        if status >= 500:
            response_text = response.text.replace("\n", " ").strip()[:160]
            logger.warning(
                "%s %s",
                OSIPTEL_REQUEST_FAILED,
                kv(
                    status=status,
                    ruc=ruc,
                    draw=draw,
                    start=start,
                    length=length,
                    body=response_text,
                ),
            )
            msg = (
                "osiptel request failed "
                f"status={status} draw={draw} start={start} length={length} "
                f"ruc={ruc} body={response_text}"
            )
            raise BanSignalError(msg)
        if status != 200:
            msg = f"osiptel request failed status={status}"
            raise TransientTransportError(msg)

        try:
            payload = response.json()
        except ValueError as exc:
            msg = "osiptel response is not valid json"
            raise ParseError(msg) from exc

        logger.info(
            "%s %s",
            FETCH_PAGE_OK,
            kv(
                ruc=ruc,
                draw=draw,
                start=start,
                length=length,
                status=status,
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            ),
        )
        return payload

    def wait_ready(self, *, timeout_s: float = 25.0, poll_s: float = 0.25) -> None:
        client = self._require_client()
        deadline = time.monotonic() + timeout_s
        last_status = 0
        last_body = ""
        last_error = ""
        while time.monotonic() < deadline:
            try:
                response = client.get(HOME_URL)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(poll_s)
                continue

            last_status = response.status_code
            last_body = response.text.replace("\n", " ").strip()[:240]
            if response.status_code == 200 and "Checa tus l" in response.text:
                self._cookie_header = _format_cookie_header(client.cookies)
                return

            if _is_waf_block(response.text):
                msg = (
                    "osiptel page blocked "
                    f"status={response.status_code} body={last_body}"
                )
                raise BanSignalError(msg)
            time.sleep(poll_s)

        msg = (
            "osiptel page not ready "
            f"status={last_status} error={last_error} body={last_body}"
        )
        raise TransientTransportError(msg)

    def _require_client(self) -> httpx.Client:
        if self._client is None:
            msg = "osiptel session not open"
            raise TransientTransportError(msg)
        return self._client

    def _api_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "*/*",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://checatuslineas.osiptel.gob.pe",
            "Referer": HOME_URL,
            "User-Agent": DEFAULT_USER_AGENT,
        }
        if self._cookie_header:
            headers["Cookie"] = self._cookie_header
        return headers


def _format_cookie_header(cookies: httpx.Cookies) -> str:
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def _is_waf_block(body: str) -> bool:
    normalized = body.lower()
    return (
        "the url you requested has been blocked" in normalized
        or "web page blocked" in normalized
        or "attack id:" in normalized
    )
