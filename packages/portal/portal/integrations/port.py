from __future__ import annotations

from typing import Protocol

from portal.domain.models import NotificationIntent


class NotificationSender(Protocol):
    """Implemented later for in-app, email, and Kapso WhatsApp delivery."""

    async def deliver(self, intent: NotificationIntent) -> None: ...
