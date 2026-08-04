from __future__ import annotations

import json

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any


if TYPE_CHECKING:
    from pathlib import Path


class DiagnosticLog:
    def __init__(self, path: Path, *, run_id: str, source: str) -> None:
        self.path = path
        self.run_id = run_id
        self.source = source
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, event: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as file:
            json.dump(
                {
                    "recordedAt": datetime.now(UTC).isoformat(),
                    "runId": self.run_id,
                    "source": self.source,
                    "event": event,
                },
                file,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            file.write("\n")
