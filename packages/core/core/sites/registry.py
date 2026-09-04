from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from core.sites.osiptel.site import OSIPTEL
from core.sites.sunat.site import SUNAT, SUNAT_REPS


if TYPE_CHECKING:
    from core.domain.types import Site


SITES: dict[str, Site] = {
    SUNAT.name: SUNAT,
    SUNAT_REPS.name: SUNAT_REPS,
    OSIPTEL.name: OSIPTEL,
}

STABLE_SITES: frozenset[str] = frozenset(
    name for name, site in SITES.items() if site.stable
)


def get_sites(names: list[str]) -> list[Site]:
    if not names:
        msg = "must list at least one site"
        raise ValueError(msg)

    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)

    if duplicates:
        msg = f"has duplicate site(s): {','.join(duplicates)}"
        raise ValueError(msg)

    unknown = [name for name in names if name not in SITES]

    if unknown:
        allowed = "|".join(sorted(SITES))
        msg = f"has unknown site(s) {','.join(unknown)}; choose from {allowed}"
        raise ValueError(msg)

    return [SITES[name] for name in names]
