from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import httpx


if TYPE_CHECKING:
    from portal.settings import PortalSettings


RESEND_URL = "https://api.resend.com/emails"
SEND_TIMEOUT_SECONDS = 10.0


class Mailer(Protocol):
    """Decides how (or whether) an email actually leaves the process."""

    async def send(self, *, to: str, subject: str, body: str) -> None: ...

    async def aclose(self) -> None: ...


class ResendMailer:
    """Talks to Resend's REST API directly, the same way CloudflareTurnstile
    calls siteverify directly rather than pulling in a vendor SDK.

    https://resend.com/docs/api-reference/emails/send-email
    """

    def __init__(self, api_key: str, mail_from: str, client: httpx.AsyncClient) -> None:
        self._api_key = api_key
        self._mail_from = mail_from
        self._client = client

    @classmethod
    def connect(cls, api_key: str, mail_from: str) -> ResendMailer:
        return cls(api_key, mail_from, httpx.AsyncClient(timeout=SEND_TIMEOUT_SECONDS))

    async def send(self, *, to: str, subject: str, body: str) -> None:
        response = await self._client.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "from": self._mail_from,
                "to": [to],
                "subject": subject,
                "text": body,
            },
        )
        response.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()


class ConsoleMailer:
    """Prints instead of sending. Development default, the same role
    HumanCheckDisabled plays for Turnstile: bootstrap already prints where to
    finish enrollment instead of emailing it, so a printed invite link is
    consistent with how this app already behaves locally.
    """

    async def send(self, *, to: str, subject: str, body: str) -> None:
        print(f"--- mail to {to} ---\nSubject: {subject}\n\n{body}\n---")

    async def aclose(self) -> None:
        return


def open_mailer(settings: PortalSettings) -> Mailer:
    if settings.resend_api_key:
        return ResendMailer.connect(settings.resend_api_key, settings.mail_from)

    return ConsoleMailer()
