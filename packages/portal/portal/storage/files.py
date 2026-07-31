from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from portal.storage.port import ObjectReference


class FileObjectStorage:
    """Durable immutable object storage rooted in the Portal's mounted volume."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, reference: ObjectReference) -> Path:
        return self._root / str(reference.id)

    async def put_immutable(self, reference: ObjectReference, content: bytes) -> None:
        if sha256(content).hexdigest() != reference.sha256:
            msg = "el contenido no coincide con la referencia inmutable"
            raise ValueError(msg)
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._path(reference)
        if path.exists():
            if path.read_bytes() != content:
                msg = "la referencia de objeto inmutable ya existe"
                raise ValueError(msg)
            return
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(content)
        temporary.replace(path)

    async def open(self, reference: ObjectReference) -> bytes:
        return self._path(reference).read_bytes()
