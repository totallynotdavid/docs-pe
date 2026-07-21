from __future__ import annotations

from typing import TYPE_CHECKING

from fetch.sites.osiptel.site import OSIPTEL
from fetch.sites.sunat.site import SUNAT, SUNAT_REPS


if TYPE_CHECKING:
    from fetch.domain.types import Site


# The registry is a plain dict, not a framework: name -> Site value. Adding a site
# is one new sites/<name>/ module plus one entry here.
SITES: dict[str, Site] = {
    SUNAT.name: SUNAT,
    SUNAT_REPS.name: SUNAT_REPS,
    OSIPTEL.name: OSIPTEL,
}


def get_sites(names: list[str]) -> list[Site]:
    if not names:
        msg = "must list at least one site"
        raise ValueError(msg)
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        msg = f"has duplicate site(s): {','.join(duplicates)}"
        raise ValueError(msg)
    unknown = [name for name in names if name not in SITES]
    if unknown:
        allowed = "|".join(sorted(SITES))
        msg = f"has unknown site(s) {','.join(unknown)}; choose from {allowed}"
        raise ValueError(msg)
    return [SITES[name] for name in names]
