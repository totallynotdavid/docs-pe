from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol
from uuid import UUID

from portal.domain.models import JobEvent


class TeamEventFeed(Protocol):
    """Future SSE route reads durable PostgreSQL events instead of process memory."""

    def events_after(self, team_id: UUID, sequence: int) -> AsyncIterator[JobEvent]: ...
