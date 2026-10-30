from __future__ import annotations

from typing import TYPE_CHECKING

from browser.sites.entel.site import ENTEL
from browser.sites.portabilidad.site import PORTABILIDAD
from browser.sites.sunat.site import SUNAT, SUNAT_REPS


if TYPE_CHECKING:
    from browser.sites.base import BrowserSite


SITES: dict[str, BrowserSite] = {
    PORTABILIDAD.name: PORTABILIDAD,
    ENTEL.name: ENTEL,
    SUNAT.name: SUNAT,
    SUNAT_REPS.name: SUNAT_REPS,
}
