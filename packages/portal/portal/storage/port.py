from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from uuid import UUID


@dataclass(frozen=True)
class ObjectReference:
    id: UUID
    team_id: UUID
    provider: str
    container: str
    object_key: str
    sha256: str
    size_bytes: int
    content_type: str


@runtime_checkable
class ObjectStorage(Protocol):
    """Stores bytes by object reference, never by filesystem path.

    Litestar runtime-checks Protocol-typed dependencies, so this protocol must
    remain runtime-checkable.
    """

    async def put_immutable(
        self,
        reference: ObjectReference,
        content: bytes,
    ) -> None: ...

    async def open(self, reference: ObjectReference) -> bytes: ...
