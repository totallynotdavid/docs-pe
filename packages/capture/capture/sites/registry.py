from __future__ import annotations

from typing import TYPE_CHECKING

from capture.sites.entel.site import ENTEL


if TYPE_CHECKING:
    from capture.sites.base import CaptureSite


# The registry is a plain dict, not a framework: name -> CaptureSite value.
# Adding a site is one new sites/<name>/ module plus one entry here.
SITES: dict[str, CaptureSite] = {
    ENTEL.name: ENTEL,
}
