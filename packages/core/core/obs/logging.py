from __future__ import annotations

import uuid


def new_session_id() -> str:
    return uuid.uuid4().hex[:10]


def kv(**fields: object) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    return " ".join(parts)
