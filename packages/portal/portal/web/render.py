from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape


def template_environment() -> Environment:
    """The future Spanish Jinja/HTMX pages share this deliberately small boundary."""
    return Environment(
        loader=FileSystemLoader(Path(__file__).with_name("templates")),
        autoescape=select_autoescape(("html", "xml")),
    )
