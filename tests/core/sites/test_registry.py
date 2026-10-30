from __future__ import annotations

import pytest

from core.sites.registry import get_sites


def test_resolves_a_site_by_name() -> None:
    assert [site.name for site in get_sites(["osiptel"])] == ["osiptel"]


def test_an_empty_selection_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one site"):
        get_sites([])


def test_duplicate_sites_are_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        get_sites(["osiptel", "osiptel"])


def test_an_unknown_site_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown site"):
        get_sites(["nope"])
