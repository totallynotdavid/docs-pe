from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from portal.storage.port import ObjectReference


class FileObjectStorage:
    """Immutable object storage rooted at a local directory."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def put_immutable(
        self,
        reference: ObjectReference,
        content: bytes,
    ) -> None:
        if sha256(content).hexdigest() != reference.sha256:
            msg = "el contenido no coincide con la referencia inmutable"
            raise ValueError(msg)

        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / str(reference.id)

        if path.exists():
            if path.read_bytes() != content:
                msg = "la referencia de objeto inmutable ya existe"
                raise ValueError(msg)
            return

        tmp_path = path.with_suffix(".tmp")
        tmp_path.write_bytes(content)
        tmp_path.replace(path)

    async def open(self, reference: ObjectReference) -> bytes:
        return (self._root / str(reference.id)).read_bytes()
