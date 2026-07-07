from __future__ import annotations

import pytest

from robot.sites.registry import get_sites


def test_resolves_sites_in_the_requested_order() -> None:
    assert [site.name for site in get_sites(["osiptel", "sunat"])] == [
        "osiptel",
        "sunat",
    ]


def test_an_empty_selection_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one site"):
        get_sites([])


def test_duplicate_sites_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        get_sites(["sunat", "sunat"])


def test_an_unknown_site_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown site"):
        get_sites(["nope"])
