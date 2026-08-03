from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from portal.web.render import COMPONENT_ASSETS_URL, COMPONENTS_DIR


router = APIRouter(prefix=COMPONENT_ASSETS_URL.rstrip("/"))

# Serve only known stylesheets. Templates stay private and path traversal is
# impossible because every filename comes from this allowlist.
_STYLESHEETS = frozenset(path.name for path in COMPONENTS_DIR.glob("*.css"))


@router.get("/{name}")
async def component_stylesheet(name: str) -> FileResponse:
    if name not in _STYLESHEETS:
        raise HTTPException(status_code=404)
    return FileResponse(COMPONENTS_DIR / name, media_type="text/css")
