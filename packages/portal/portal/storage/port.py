from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True)
class ObjectReference:
    """An immutable, provider-neutral object pointer stored in PostgreSQL."""

    id: UUID
    team_id: UUID
    provider: str
    container: str
    object_key: str
    sha256: str
    size_bytes: int
    content_type: str


class ObjectStorage(Protocol):
    """Future adapter boundary; it never accepts a local process path."""

    async def put_immutable(
        self, reference: ObjectReference, content: bytes
    ) -> None: ...

    async def open(self, reference: ObjectReference) -> bytes: ...
