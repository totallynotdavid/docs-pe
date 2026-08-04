from __future__ import annotations

from typing import TYPE_CHECKING

from capture.sites.entel.site import ENTEL


if TYPE_CHECKING:
    from capture.sites.base import CaptureSite

SITES: dict[str, CaptureSite] = {
    ENTEL.name: ENTEL,
}
