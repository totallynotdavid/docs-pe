from __future__ import annotations

import argparse

from typing import TYPE_CHECKING

from portal.credentials.masterkey import new_master_key_line


if TYPE_CHECKING:
    from collections.abc import Sequence


def run(argv: Sequence[str]) -> None:
    """Print one keyring line. Prepending it to the key file rotates the key.

    Deliberately writes to stdout rather than editing the file: the operator
    decides where the key file lives and who may read it, and a command that
    rewrites it in place would have to guess both.
    """
    parser = argparse.ArgumentParser(
        prog="portal new-key",
        description="Print a master key line for PORTAL_MASTER_KEY_FILE.",
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Name for this key, recorded on every secret it wraps (e.g. v2).",
    )

    print(new_master_key_line(parser.parse_args(argv).version))
