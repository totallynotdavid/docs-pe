import hashlib

from pathlib import Path


COMPONENTS_DIR = Path(__file__).with_name("components")
PAGES_DIR = Path(__file__).with_name("pages")
STATIC_DIR = Path(__file__).with_name("static")


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
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    filename = f"components.{digest}.css"
    path = static_dir / filename

    if not path.exists():
        static_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    # Bundles from earlier content hashes are never requested again, and a
    # stale one left in the tree reads like current CSS to anyone grepping it:
    # a rule deleted from a component still turns up in the build output.
    for old_bundle in static_dir.glob("components.*.css"):
        if old_bundle != path:
            old_bundle.unlink()

    return f"/static/{filename}"
