from __future__ import annotations

from typing import TYPE_CHECKING

from browser.sites.entel.site import ENTEL
from browser.sites.portabilidad.site import PORTABILIDAD


if TYPE_CHECKING:
    from browser.sites.base import BrowserSite


SITES: dict[str, BrowserSite] = {
    PORTABILIDAD.name: PORTABILIDAD,
    ENTEL.name: ENTEL,
}
