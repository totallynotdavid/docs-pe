from __future__ import annotations

from portal.storage.port import ObjectReference


class InMemoryObjectStorage:
    """Small adapter for integration tests; deployments inject their own port."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}

    async def put_immutable(self, reference: ObjectReference, content: bytes) -> None:
        existing = self._objects.get(str(reference.id))
        if existing is not None and existing != content:
            msg = "la referencia de objeto inmutable ya existe"
            raise ValueError(msg)
        self._objects[str(reference.id)] = content

    async def open(self, reference: ObjectReference) -> bytes:
        return self._objects[str(reference.id)]


class UnconfiguredObjectStorage:
    """Makes a missing production object-store adapter explicit and safe."""

    async def put_immutable(self, reference: ObjectReference, content: bytes) -> None:
        del reference, content
        msg = "el almacenamiento de objetos del portal no está configurado"
        raise RuntimeError(msg)

    async def open(self, reference: ObjectReference) -> bytes:
        del reference
        msg = "el almacenamiento de objetos del portal no está configurado"
        raise RuntimeError(msg)
