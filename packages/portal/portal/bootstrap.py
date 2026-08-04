from __future__ import annotations

import argparse
import asyncio
import os

from portal.provision import provision


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()

    if not value:
        raise RuntimeError(f"la variable {name} es obligatoria para el arranque local")

    return value


def _env_or_default(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


async def bootstrap() -> None:
    await provision(
        argparse.Namespace(
            admin_email=_required_env("PORTAL_BOOTSTRAP_ADMIN_EMAIL"),
            admin_password_env="PORTAL_BOOTSTRAP_ADMIN_PASSWORD",
            team_name=_env_or_default(
                "PORTAL_BOOTSTRAP_TEAM_NAME",
                "Equipo Lima",
            ),
            team_slug=_env_or_default(
                "PORTAL_BOOTSTRAP_TEAM_SLUG",
                "equipo-lima",
            ),
            proxy_provider=None,
            proxy_label="Principal",
        )
    )


def main() -> None:
    try:
        asyncio.run(bootstrap())
    except Exception as error:
        raise SystemExit(f"Arranque local no completado: {error}") from error


if __name__ == "__main__":
    main()
