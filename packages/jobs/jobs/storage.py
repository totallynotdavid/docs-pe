from __future__ import annotations

import hashlib
import os
import tempfile

from pathlib import Path


class LocalObjectStore:
    """Immutable local object-store adapter used in development and tests.

    The database stores only object keys and checksums. Production can replace this
    class with an object-store adapter without changing job, export, or audit rows.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put_immutable(self, *, namespace: str, content: bytes) -> tuple[str, str]:
        checksum = hashlib.sha256(content).hexdigest()
        folder = self.root / namespace / checksum[:2]
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{checksum}-{os.urandom(8).hex()}"
        target = folder / name
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(target)
        return str(target.relative_to(self.root)), checksum

    def read(self, key: str) -> bytes:
        return (self.root / key).read_bytes()
