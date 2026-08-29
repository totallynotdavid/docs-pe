from __future__ import annotations

import argparse
import asyncio
import sys

from typing import TYPE_CHECKING

from core.domain.errors import FetchError, ProxyConfigurationError
from core.proxy.base import values_from_environment
from core.proxy.registry import PROVIDERS, preflight, spec_for


if TYPE_CHECKING:
    from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fetch-preflight",
        description="Check one configured proxy provider and report its exit IP.",
    )
    parser.add_argument(
        "--provider",
        required=True,
        choices=sorted(PROVIDERS),
        help="provider name whose environment configuration will be tested",
    )
    return parser


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


async def run(provider_name: str) -> int:
    spec = spec_for(provider_name)

    try:
        values = values_from_environment(spec)
        exit_ip = await preflight(provider_name, values)
    except ProxyConfigurationError as error:
        print(f"{provider_name}: preflight failed: {error}", file=sys.stderr)
        return 1
    except FetchError as error:
        print(
            f"{provider_name}: preflight failed: {type(error).__name__}",
            file=sys.stderr,
        )
        return 1

    print(f"{provider_name}: exit IP {exit_ip}")
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    args = parse_args(argv)
    raise SystemExit(asyncio.run(run(args.provider)))


if __name__ == "__main__":
    main()
