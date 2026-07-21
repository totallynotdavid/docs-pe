from __future__ import annotations

from typing import TYPE_CHECKING

from browser.sites.base import BrowserSite
from browser.sites.entel.page import URL, EntelPage


if TYPE_CHECKING:
    from browser.controller import PageController
    from browser.diagnostics import DiagnosticLog


def _open_page(
    controller: PageController,
    *,
    control_ruc: str,
    reset_cookies: bool,
    diagnostic_log: DiagnosticLog | None,
) -> EntelPage:
    page = EntelPage(
        controller=controller,
        control_ruc=control_ruc,
        reset_cookies=reset_cookies,
        diagnostic_log=diagnostic_log,
    )
    page.prepare()
    return page


def _row(ruc: str, columns: dict[str, str], observed_at: str) -> list[str]:
    return [ruc, columns["debt_total"], columns["has_punishment"], observed_at]


ENTEL = BrowserSite(
    name="entel",
    url=URL,
    export_header=("ruc", "debt_total", "has_punishment", "observed_at"),
    open_page=_open_page,
    row=_row,
)
