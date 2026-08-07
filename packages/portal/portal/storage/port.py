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
    """Stores bytes by object reference, never by local filesystem path.

    Litestar's signature model isinstance-checks every dependency against its
    declared parameter type, including Protocol-typed ones, so this needs to
    stay runtime-checkable for `storage: ObjectStorage` handler parameters.
    """

    async def put_immutable(
        self,
        reference: ObjectReference,
        content: bytes,
    ) -> None: ...

    async def open(self, reference: ObjectReference) -> bytes: ...
