from __future__ import annotations

import sys

from collections.abc import Sequence
from importlib import import_module


COMMANDS: dict[str, tuple[str, str]] = {
    "migrate": ("portal.admin.migrate", "Apply pending schema migrations."),
    "provision": (
        "portal.admin.provision",
        "Create or verify the initial installation.",
    ),
    "bootstrap": (
        "portal.admin.bootstrap",
        "Provision the local development installation.",
    ),
    "worker": ("portal.admin.workers", "Issue or revoke worker credentials."),
    "key": ("portal.admin.keys", "Manage the master-key file."),
}


def _usage() -> str:
    width = max(len(name) for name in COMMANDS)
    lines = [
        f"  {name.ljust(width)}  {description}"
        for name, (_, description) in COMMANDS.items()
    ]

    return "\n".join(["Usage: portal-admin <command> [options]", "", *lines])


def main(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in {"-h", "--help"}:
        print(_usage())
        return

    command, rest = args[0], args[1:]
    entry = COMMANDS.get(command)

    if entry is None:
        raise SystemExit(f"unknown command '{command}'\n\n{_usage()}")

    module = import_module(entry[0])
    module.run(rest)


if __name__ == "__main__":
    main()
