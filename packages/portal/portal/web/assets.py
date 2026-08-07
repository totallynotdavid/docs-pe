from __future__ import annotations

import hashlib

from pathlib import Path


COMPONENTS_DIR = Path(__file__).with_name("components")
PAGES_DIR = Path(__file__).with_name("pages")
STATIC_DIR = Path(__file__).with_name("static")


def build_component_stylesheet(
    components_dir: Path = COMPONENTS_DIR,
    static_dir: Path = STATIC_DIR,
) -> str:
    """Bundle every component stylesheet into one content-hashed file, once.

    JinjaX's own ``catalog.render_assets()`` instead emits one ``<link>`` per
    ``{#css#}`` declaration reachable from the render tree, served through a
    per-file allowlist route: one HTTP request per component stylesheet, per
    page load, and a stylesheet missing from an htmx-swapped fragment's tree
    even though the browser already needs it. A single bundle sidesteps both:
    every page always loads every component style, so there's no tree to get
    wrong, and it costs one cached request instead of N.
    """
    sheets = sorted(components_dir.glob("*.css"))
    content = "\n".join(sheet.read_text(encoding="utf-8") for sheet in sheets)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    filename = f"components.{digest}.css"
    path = static_dir / filename

    if not path.exists():
        static_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    return f"/static/{filename}"
