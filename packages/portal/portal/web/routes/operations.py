from __future__ import annotations

from fastapi import APIRouter, Response

from portal.web.deps import Readiness


router = APIRouter(tags=["operación"])


@router.get("/salud")
async def health() -> dict[str, str]:
    return {"estado": "saludable"}


@router.get("/listo")
async def ready(readiness: Readiness, response: Response) -> dict[str, str]:
    if not await readiness.ready():
        response.status_code = 503
        return {"estado": "no_listo"}
    return {"estado": "listo"}
