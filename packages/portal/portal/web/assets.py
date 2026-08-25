import hashlib

from pathlib import Path


COMPONENTS_DIR = Path(__file__).with_name("components")
PAGES_DIR = Path(__file__).with_name("pages")
STATIC_DIR = Path(__file__).with_name("static")


def _hashed_stylesheet(content: str, prefix: str, static_dir: Path) -> str:
    """Write `content` under a content-hashed filename."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    filename = f"{prefix}.{digest}.css"
    path = static_dir / filename

    if not path.exists():
        static_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    # Keep only the bundle referenced by the current content hash.
    for old_bundle in static_dir.glob(f"{prefix}.*.css"):
        if old_bundle != path:
            old_bundle.unlink()

    return f"/static/{filename}"


def build_component_stylesheet(
    components_dir: Path = COMPONENTS_DIR,
    static_dir: Path = STATIC_DIR,
) -> str:
    """Bundle component styles into one content-hashed stylesheet.

    This avoids JinjaX's per-component requests and ensures styles needed by
    htmx-swapped fragments are always loaded.
    """
    sheets = sorted(components_dir.glob("*.css"))
    content = "\n".join(sheet.read_text(encoding="utf-8") for sheet in sheets)

    return _hashed_stylesheet(content, "components", static_dir)


def build_tokens_stylesheet(static_dir: Path = STATIC_DIR) -> str:
    """Serve the design tokens under a content-hashed name."""
    content = (static_dir / "tokens.css").read_text(encoding="utf-8")

    return _hashed_stylesheet(content, "tokens", static_dir)
