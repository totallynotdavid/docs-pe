import hashlib

from pathlib import Path


COMPONENTS_DIR = Path(__file__).with_name("components")
PAGES_DIR = Path(__file__).with_name("pages")
STATIC_DIR = Path(__file__).with_name("static")


def _hashed_stylesheet(content: str, prefix: str, static_dir: Path) -> str:
    """Write `content` under a filename that changes whenever it does.

    A browser or Cloudflare's edge otherwise caches a plain filename across a
    deploy that changes its content: tokens.css once sat at max-age=14400 with
    no way to bust that, so a token or reset-layer change could take up to 4
    hours to reach anyone with a warm cache.
    """
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    filename = f"{prefix}.{digest}.css"
    path = static_dir / filename

    if not path.exists():
        static_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    # Bundles from earlier content hashes are never requested again, and a
    # stale one left in the tree reads like current CSS to anyone grepping it:
    # a rule deleted from a component still turns up in the build output.
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
    """Serve tokens.css under a content-hashed name; see `_hashed_stylesheet`."""
    content = (static_dir / "tokens.css").read_text(encoding="utf-8")

    return _hashed_stylesheet(content, "tokens", static_dir)
