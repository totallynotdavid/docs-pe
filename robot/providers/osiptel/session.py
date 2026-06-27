from __future__ import annotations

import asyncio
import logging
import time

from typing import Any

import httpx

from robot.domain.errors import (
    BanSignalError,
    ParseError,
    SessionStateError,
    TransientTransportError,
    UpstreamNotReadyError,
)
from robot.obs.events import FETCH_PAGE_OK, FETCH_PAGE_START, OSIPTEL_REQUEST_FAILED
from robot.obs.logging import kv, new_session_id


logger = logging.getLogger(__name__)

HOME_URL = "https://checatuslineas.osiptel.gob.pe/"
API_URL = "https://checatuslineas.osiptel.gob.pe/Consultas/GetAllCabeceraConsulta/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_S = 45.0


def build_client(*, proxy_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        proxy=proxy_url,
        timeout=REQUEST_TIMEOUT_S,
        headers={"User-Agent": DEFAULT_USER_AGENT},
        follow_redirects=True,
    )


class OsiptelSession:
    """OSIPTEL request protocol bound to a ready httpx client.

    The proxy lives in the client the caller passes in; this type only knows how
    to make the upstream ready and classify its responses, so it does not need
    to know anything about GeoNode.
    """

    def __init__(self, *, client: httpx.AsyncClient) -> None:
        self._client: httpx.AsyncClient | None = client
        self._cookie_header = ""
        self.session_id = new_session_id()

    async def close(self) -> None:
        if self._client is None:
            return
        await self._client.aclose()
        self._client = None

    async def fetch_json(
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
            response = await client.post(
                API_URL, data=data, headers=self._api_headers()
            )
        except httpx.HTTPError as exc:
            msg = f"osiptel request transport failed: {type(exc).__name__}: {exc}"
            raise TransientTransportError(msg) from exc

        status = response.status_code
        if status in _TRANSIENT_UPSTREAM_STATUSES:
            self._log_failed(
                status,
                ruc=ruc,
                draw=draw,
                start=start,
                length=length,
                body=response.text,
            )
            msg = f"osiptel request transient status={status}"
            raise TransientTransportError(msg)
        if status >= 500 or status in _BAN_SIGNAL_STATUSES:
            body = self._log_failed(
                status,
                ruc=ruc,
                draw=draw,
                start=start,
                length=length,
                body=response.text,
            )
            msg = (
                "osiptel request failed "
                f"status={status} draw={draw} start={start} length={length} "
                f"ruc={ruc} body={body}"
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

    async def wait_ready(
        self, *, timeout_s: float = 25.0, poll_s: float = 0.25
    ) -> None:
        client = self._require_client()
        deadline = time.monotonic() + timeout_s
        last_status = 0
        last_body = ""
        last_error = ""
        while time.monotonic() < deadline:
            try:
                response = await client.get(HOME_URL)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(poll_s)
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
            await asyncio.sleep(poll_s)

        msg = (
            "osiptel page not ready "
            f"status={last_status} error={last_error} body={last_body}"
        )
        raise UpstreamNotReadyError(msg)

    def _log_failed(
        self, status: int, *, ruc: str, draw: int, start: int, length: int, body: str
    ) -> str:
        text = body.replace("\n", " ").strip()[:160]
        logger.warning(
            "%s %s",
            OSIPTEL_REQUEST_FAILED,
            kv(
                status=status, ruc=ruc, draw=draw, start=start, length=length, body=text
            ),
        )
        return text

    def _require_client(self) -> httpx.AsyncClient:
        if self._client is None:
            msg = "osiptel session not open"
            raise SessionStateError(msg)
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


# 502/503/504 mean the upstream is degraded; the proxy is probably fine, so the
# retry policy keeps the same session. Other 5xx and the ban-shaped 4xx codes
# flip to BanSignalError, which rotates and cools the proxy down.
_TRANSIENT_UPSTREAM_STATUSES = frozenset({502, 503, 504})
_BAN_SIGNAL_STATUSES = frozenset({403, 429})


def _is_waf_block(body: str) -> bool:
    normalized = body.lower()
    return (
        "the url you requested has been blocked" in normalized
        or "web page blocked" in normalized
        or "attack id:" in normalized
    )
