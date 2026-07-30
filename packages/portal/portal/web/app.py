from __future__ import annotations

import os

from dataclasses import dataclass
from typing import Protocol

from fastapi import FastAPI, Response


class ReadinessProbe(Protocol):
    """Small infrastructure boundary so readiness never depends on a UI route."""

    async def ready(self) -> bool: ...


@dataclass(frozen=True)
class PortalSettings:
    database_dsn: str

    @classmethod
    def from_environment(cls) -> PortalSettings:
        return cls(database_dsn=os.environ.get("PORTAL_DATABASE_DSN", ""))


class DatabaseConfigured:
    """Foundation readiness probe; later replaces this with an asyncpg ping."""

    def __init__(self, settings: PortalSettings) -> None:
        self._settings = settings

    async def ready(self) -> bool:
        return bool(self._settings.database_dsn)


def create_app(
    settings: PortalSettings | None = None, readiness: ReadinessProbe | None = None
) -> FastAPI:
    """Create only operational routes; browser pages deliberately arrive later."""
    settings = settings or PortalSettings.from_environment()
    readiness = readiness or DatabaseConfigured(settings)
    app = FastAPI(title="Portal OSIPTEL", version="0.1.0")
    app.state.settings = settings

    @app.get("/salud", tags=["operación"])
    async def health() -> dict[str, str]:
        return {"estado": "saludable"}

    @app.get("/listo", tags=["operación"])
    async def ready(response: Response) -> dict[str, str]:
        if not await readiness.ready():
            response.status_code = 503
            return {"estado": "no_listo"}
        return {"estado": "listo"}

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8000)
