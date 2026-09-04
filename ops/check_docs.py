#!/usr/bin/env python3
"""Check links, repository paths, and selected source-owned documentation contracts."""

from __future__ import annotations

import re
import sys

from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_LINK = re.compile(r"(?<!!)\[[^\]]+\]\(([^)\s]+)")
# Repository paths in prose are easy to leave stale because Markdown link
# checks do not inspect inline code spans.
CODE_PATH = re.compile(r"`((?:docs|packages|ops)/[^`\s]+|docker-compose\.[^`\s]+)`")
PROVIDER_FIELD = re.compile(r'Field\("([a-z0-9_]+)"')
PROVIDER_ENV_FIELD = re.compile(r"\b([A-Z][A-Z0-9]+)_([A-Z][A-Z0-9_]*)\b")
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

    if destination.is_dir() and not (destination / "readme.md").is_file():
        return (
            f"{source.relative_to(ROOT)}:{line}: "
            f"linked directory has no readme.md: {target}"
        )

    if (
        fragment
        and destination.is_file()
        and fragment not in heading_anchors(destination)
    ):
        return f"{source.relative_to(ROOT)}:{line}: missing heading anchor: {target}"

    return None


def check_code_path(source: Path, target: str, line: int) -> str | None:
    if any(marker in target for marker in ("<", ">", "*", "{")):
        return None

    destination = (ROOT / target.rstrip("/")).resolve()
    try:
        destination.relative_to(ROOT)
    except ValueError:
        return f"{source.relative_to(ROOT)}:{line}: path escapes repository: {target}"

    if not destination.exists():
        return f"{source.relative_to(ROOT)}:{line}: missing code path: {target}"

    return None


def check_contract_docs() -> list[str]:
    errors: list[str] = []

    required_text = {
        ROOT / "docs/operations/portal-deployment.md": (
            "portal_schema_migrations",
            "mise run portal:schema",
            "PORTAL_WORKER_API_WORKERS",
            "portal_lookup_attempts",
        ),
        ROOT / "docs/operations/troubleshooting.md": (
            "portal_lookup_attempts",
            "fetch_attempt",
        ),
        ROOT / "docs/portal.md": (
            "Reutilizar y consultar solo lo nuevo",
            "global search",
            "second factor",
        ),
        ROOT / "docs/input-format.md": (
            "first column",
            "2953322",
            "02953322",
        ),
    }

    for path, needles in required_text.items():
        contents = " ".join(path.read_text(encoding="utf-8").split()).casefold()
        for needle in needles:
            if needle.casefold() not in contents:
                errors.append(
                    f"{path.relative_to(ROOT)}: missing contract text: {needle}"
                )

    proxy_docs = (ROOT / "docs/proxies.md").read_text(encoding="utf-8")
    proxy_dir = ROOT / "packages/core/core/proxy"
    for path in sorted(proxy_dir.glob("*.py")):
        contents = path.read_text(encoding="utf-8")
        if "ProviderSpec(" not in contents:
            continue
        provider = path.stem.upper()
        source_link = f"../packages/core/core/proxy/{path.name}"
        if source_link not in proxy_docs:
            errors.append(f"docs/proxies.md: missing source link: {source_link}")

        source_fields = set(PROVIDER_FIELD.findall(contents))
        for field in PROVIDER_FIELD.findall(contents):
            env_name = f"{provider}_{field.upper()}"
            if env_name not in proxy_docs:
                errors.append(
                    f"docs/proxies.md: missing provider field reference: {env_name}"
                )

        documented_fields = {
            suffix.rstrip("_").lower()
            for prefix, suffix in PROVIDER_ENV_FIELD.findall(proxy_docs)
            if prefix == provider
        }
        for field in sorted(documented_fields - source_fields):
            errors.append(
                f"docs/proxies.md: stale provider field reference: {provider}_{field.upper()}"
            )

    return errors


def main() -> int:
    errors: list[str] = []

    for source in markdown_files():
        contents = source.read_text(encoding="utf-8")
        for match in MARKDOWN_LINK.finditer(contents):
            line = contents.count("\n", 0, match.start()) + 1
            error = check_link(source, match.group(1), line)
            if error:
                errors.append(error)

        for match in CODE_PATH.finditer(contents):
            line = contents.count("\n", 0, match.start()) + 1
            error = check_code_path(source, match.group(1), line)
            if error:
                errors.append(error)

    errors.extend(check_contract_docs())

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1

    print(f"checked {len(markdown_files())} Markdown files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
