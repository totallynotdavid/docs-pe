from __future__ import annotations

import argparse
import asyncio
import os

from portal.provision import provision


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        msg = f"la variable {name} es obligatoria para el arranque local"
        raise RuntimeError(msg)
    return value


async def bootstrap() -> None:
    """Idempotently create the local administrator and first team."""
    await provision(
        argparse.Namespace(
            admin_email=_required("PORTAL_BOOTSTRAP_ADMIN_EMAIL"),
            admin_password_env="PORTAL_BOOTSTRAP_ADMIN_PASSWORD",
            team_name=os.environ.get("PORTAL_BOOTSTRAP_TEAM_NAME", "Equipo Lima"),
            team_slug=os.environ.get("PORTAL_BOOTSTRAP_TEAM_SLUG", "equipo-lima"),
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
