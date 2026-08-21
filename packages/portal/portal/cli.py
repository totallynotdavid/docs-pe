from __future__ import annotations

import sys

from collections.abc import Callable, Sequence
from importlib import import_module


# One command with subcommands rather than seven console scripts. The workspace
# shares a virtualenv with fetch, so unprefixed names like `migrate` or `worker`
# would be ambiguous on PATH, and `portal-` repeated seven times is the same
# word said seven times.
#
# Each entry is imported only when it is the one being run: `portal migrate`
# should not pay for litestar, and `portal web` should not pay for the fetch
# pipeline the agent pulls in.
COMMANDS: dict[str, tuple[str, str]] = {
    "web": ("portal.web.app", "Serve the browser-facing app."),
    "worker-api": ("portal.worker.api", "Serve the tailnet-only worker API."),
    "worker": ("portal.worker.agent", "Claim and run work on a worker node."),
    "migrate": ("portal.migrate", "Apply pending schema migrations."),
    "provision": ("portal.provision", "Create or verify the initial installation."),
    "bootstrap": ("portal.bootstrap", "Provision from PORTAL_BOOTSTRAP_* (local dev)."),
    "enroll-worker": (
        "portal.worker.enrollment",
        "Issue or revoke a worker credential.",
    ),
    "new-key": ("portal.newkey", "Print a master key line for the key file."),
    "rewrap": ("portal.rewrap", "Move stored secrets onto the active master key."),
}


def _usage() -> str:
    width = max(len(name) for name in COMMANDS)
    lines = [
        f"  {name.ljust(width)}  {help_text}"
        for name, (_, help_text) in COMMANDS.items()
    ]

    return "\n".join(["Usage: portal <command> [options]", "", *lines])


def main() -> None:
    argv = sys.argv[1:]

    if not argv or argv[0] in {"-h", "--help"}:
        print(_usage())
        return

    command, rest = argv[0], argv[1:]
    entry = COMMANDS.get(command)

    if entry is None:
        raise SystemExit(f"unknown command '{command}'\n\n{_usage()}")

    module, _ = entry
    run: Callable[[Sequence[str]], None] = import_module(module).run

    run(rest)


if __name__ == "__main__":
    main()
