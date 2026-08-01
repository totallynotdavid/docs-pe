from __future__ import annotations


def sse_event(*, event: str, data: str, event_id: int | None = None) -> str:
    """Frame one server-sent event.

    Every line of `data` needs its own `data:` prefix, otherwise a multi-line
    payload such as a rendered fragment ends the event at its first newline.
    """
    lines = [] if event_id is None else [f"id: {event_id}"]
    lines.append(f"event: {event}")
    lines.extend(f"data: {line}" for line in data.split("\n"))
    return "\n".join(lines) + "\n\n"
