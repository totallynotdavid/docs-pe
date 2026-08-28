#!/usr/bin/env python3
"""Check local Markdown links and heading anchors."""

from __future__ import annotations

import re
import sys

from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)")
HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*#*\s*$")


def markdown_files() -> list[Path]:
    ignored = {".git", ".venv", "node_modules"}
    return sorted(
        path for path in ROOT.rglob("*.md") if not ignored.intersection(path.parts)
    )


def heading_anchors(path: Path) -> set[str]:
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    in_fence = False

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        match = HEADING.match(line)
        if not match:
            continue

        slug = re.sub(r"[^\w\s-]", "", match.group(1).lower())
        slug = re.sub(r"[\s-]+", "-", slug).strip("-")
        occurrence = counts.get(slug, 0)
        counts[slug] = occurrence + 1
        anchors.add(slug if occurrence == 0 else f"{slug}-{occurrence}")

    return anchors


def check_link(source: Path, target: str, line: int) -> str | None:
    if target.startswith(("http://", "https://", "mailto:", "tel:")):
        return None

    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc:
        return None

    fragment = unquote(parsed.fragment).lower()
    relative = unquote(parsed.path)
    destination = source.parent / relative if relative else source
    destination = destination.resolve()

    try:
        destination.relative_to(ROOT)
    except ValueError:
        return f"{source.relative_to(ROOT)}:{line}: link escapes repository: {target}"

    if not destination.exists():
        return f"{source.relative_to(ROOT)}:{line}: missing link target: {target}"

    if (
        fragment
        and destination.is_file()
        and fragment not in heading_anchors(destination)
    ):
        return f"{source.relative_to(ROOT)}:{line}: missing heading anchor: {target}"

    return None


def main() -> int:
    errors: list[str] = []

    for source in markdown_files():
        contents = source.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK.finditer(contents):
            line = contents.count("\n", 0, match.start()) + 1
            error = check_link(source, match.group(1), line)
            if error:
                errors.append(error)

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1

    print(f"checked {len(markdown_files())} Markdown files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
