from __future__ import annotations

import argparse
import os
import tempfile

from collections.abc import Sequence
from pathlib import Path

from portal.admin import rewrap as rewrap_command
from portal.credentials.masterkey import MasterKeyring, new_master_key_line


def _generated_line(version: str) -> str:
    line = new_master_key_line(version)
    MasterKeyring.from_lines([line], source="generated key")
    return line


def _atomic_write(path: Path, contents: str, *, replace: bool) -> None:
    if not path.parent.is_dir():
        raise RuntimeError(f"parent directory does not exist: {path.parent}")

    if not replace and (path.exists() or path.is_symlink()):
        raise RuntimeError(f"refusing to overwrite existing key file: {path}")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(contents)
            if not contents.endswith("\n"):
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        if replace:
            temporary.replace(path)
        else:
            os.link(temporary, path)
            temporary.unlink()
    except OSError as error:
        raise RuntimeError(f"could not write key file {path}: {error}") from error
    finally:
        if temporary.exists():
            temporary.unlink()


def _install(path: Path, version: str) -> None:
    _atomic_write(path, _generated_line(version), replace=False)
    print(f"Installed master key {version} at {path}")


def _rotate(path: Path, version: str) -> None:
    if not path.is_file():
        raise RuntimeError(f"key file does not exist: {path}")

    current = path.read_text(encoding="utf-8")
    MasterKeyring.from_lines(current.splitlines(), source=str(path))
    existing_versions = {
        line.split(maxsplit=1)[0]
        for line in current.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    if version in existing_versions:
        raise RuntimeError(f"key version already exists: {version}")

    _atomic_write(path, f"{_generated_line(version)}\n{current}", replace=True)
    print(f"Installed master key {version} at {path}; active key is now {version}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="portal-admin key",
        description="Manage the portal master-key file.",
    )
    actions = parser.add_subparsers(dest="action", required=True)

    generate = actions.add_parser("generate", help="print one keyring line")
    generate.add_argument("--version", required=True)

    for action, help_text in (
        ("install", "install the first key in a new file"),
        ("rotate", "prepend a new key to an existing file"),
    ):
        command = actions.add_parser(action, help=help_text)
        command.add_argument("--path", type=Path, required=True)
        command.add_argument("--version", required=True)

    actions.add_parser("rewrap", help="rewrap stored secrets with the active key")
    return parser


def run(argv: Sequence[str]) -> None:
    args = build_parser().parse_args(argv)

    try:
        if args.action == "generate":
            print(_generated_line(args.version))
        elif args.action == "install":
            _install(args.path, args.version)
        elif args.action == "rotate":
            _rotate(args.path, args.version)
        else:
            rewrap_command.run(())
    except Exception as error:
        raise SystemExit(f"Key administration did not complete: {error}") from error
