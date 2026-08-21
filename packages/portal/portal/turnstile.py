from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import httpx


if TYPE_CHECKING:
    from portal.settings import PortalSettings


SITEVERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
WIDGET_SCRIPT_ORIGIN = "https://challenges.cloudflare.com"

VERIFY_TIMEOUT_SECONDS = 5.0


class HumanCheck(Protocol):
    """Decides whether a login attempt came from a browser a person is using."""

    async def passed(self, *, token: str, client_ip: str) -> bool: ...

    async def aclose(self) -> None: ...


class CloudflareTurnstile:
    """Server-side siteverify. A widget token alone proves nothing.

    https://developers.cloudflare.com/turnstile/
    """

    def __init__(self, secret: str, client: httpx.AsyncClient) -> None:
        self._secret = secret
        self._client = client

    @classmethod
    def connect(cls, secret: str) -> CloudflareTurnstile:
        return cls(secret, httpx.AsyncClient(timeout=VERIFY_TIMEOUT_SECONDS))

    async def passed(self, *, token: str, client_ip: str) -> bool:
        if not token:
            return False

        try:
            response = await self._client.post(
                SITEVERIFY_URL,
                data={
                    "secret": self._secret,
                    "response": token,
                    "remoteip": client_ip,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError:
            # Fail closed. An unreachable siteverify makes logins unavailable,
            # which is the failure we want over an open door during an outage.
            return False

        return bool(response.json().get("success"))

    async def aclose(self) -> None:
        await self._client.aclose()


class HumanCheckDisabled:
    """Accepts every attempt. Development only, refused by settings.validate()."""

    async def passed(self, *, token: str, client_ip: str) -> bool:
        del token, client_ip

        return True

    async def aclose(self) -> None:
        return


def open_human_check(settings: PortalSettings) -> HumanCheck:
    if settings.turnstile_secret:
        return CloudflareTurnstile.connect(settings.turnstile_secret)

    return HumanCheckDisabled()
