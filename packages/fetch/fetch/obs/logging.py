from __future__ import annotations

import logging
import sys
import uuid

from pathlib import Path


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def new_session_id() -> str:
    return uuid.uuid4().hex[:10]


def configure_logging(*, debug: bool, run_id: str | None = None) -> None:
    root_level = logging.INFO
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(root_level)

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")

    console = logging.StreamHandler(stream=sys.stdout)
    console.setLevel(root_level)
    console.setFormatter(formatter)
    root.addHandler(console)

    if run_id:
        log_dir = Path("logs")
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{run_id}.log"
        file_handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        file_handler.setLevel(root_level)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    if debug:
        logging.getLogger("fetch").setLevel(logging.DEBUG)

    noisy_loggers = (
        "httpx",
        "httpcore",
    )
    for name in noisy_loggers:
        logging.getLogger(name).setLevel(logging.WARNING)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)


def kv(**fields: object) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    return " ".join(parts)
