from __future__ import annotations

import subprocess

from typing import TYPE_CHECKING

from ops.check_docs import markdown_files


if TYPE_CHECKING:
    from pathlib import Path


def git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def write(root: Path, name: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Title\n", encoding="utf-8")


def test_markdown_files_skip_ignored_and_generated_trees(tmp_path: Path) -> None:
    git(tmp_path, "init", "--quiet")
    (tmp_path / ".gitignore").write_text(".pytest_cache/\n.venv/\n", encoding="utf-8")
    for name in (
        "README.md",
        "docs/guide.md",
        ".pytest_cache/README.md",
        ".venv/lib/site-packages/pkg/README.md",
    ):
        write(tmp_path, name)

    assert markdown_files(tmp_path) == [
        tmp_path / "README.md",
        tmp_path / "docs/guide.md",
    ]


def test_markdown_files_include_new_files_and_drop_deleted_ones(
    tmp_path: Path,
) -> None:
    git(tmp_path, "init", "--quiet")
    write(tmp_path, "old.md")
    git(tmp_path, "add", "old.md")
    (tmp_path / "old.md").unlink()
    write(tmp_path, "new.md")

    assert markdown_files(tmp_path) == [tmp_path / "new.md"]
