from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from portal.web.render import COMPONENT_ASSETS_URL, COMPONENTS_DIR


router = APIRouter(prefix=COMPONENT_ASSETS_URL.rstrip("/"))

# A stylesheet sits next to the component template that owns it, so the folder
# cannot simply be mounted: that would serve the templates too. Reading the names
# once turns the route into an allowlist, which is also what makes a traversing
# path impossible to express.
_STYLESHEETS = frozenset(path.name for path in COMPONENTS_DIR.glob("*.css"))


@router.get("/{name}")
async def component_stylesheet(name: str) -> FileResponse:
    if name not in _STYLESHEETS:
        raise HTTPException(status_code=404)
    return FileResponse(COMPONENTS_DIR / name, media_type="text/css")
