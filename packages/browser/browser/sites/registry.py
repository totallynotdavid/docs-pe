from __future__ import annotations

from typing import TYPE_CHECKING

from browser.sites.entel.site import ENTEL


if TYPE_CHECKING:
    from browser.sites.base import BrowserSite


# The registry is a plain dict, not a framework: name -> BrowserSite value.
# Adding a site is one new sites/<name>/ module plus one entry here.
SITES: dict[str, BrowserSite] = {
    ENTEL.name: ENTEL,
}
