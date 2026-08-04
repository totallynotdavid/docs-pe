from __future__ import annotations


def sse_event(*, event: str, data: str, event_id: int | None = None) -> str:
    """Frame one server-sent event.

    Each line of a multi-line payload needs its own `data:` field.
    """
    lines = [] if event_id is None else [f"id: {event_id}"]
    lines.append(f"event: {event}")
    lines.extend(f"data: {line}" for line in data.split("\n"))
    return "\n".join(lines) + "\n\n"
