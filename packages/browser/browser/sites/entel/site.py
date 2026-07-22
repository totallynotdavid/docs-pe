from __future__ import annotations

from typing import TYPE_CHECKING

from browser.sites.base import BrowserSite
from browser.sites.entel.page import URL, EntelPage
from browser.subject import SubjectKind


if TYPE_CHECKING:
    from browser.diagnostics import DiagnosticLog
    from browser.session import Session


_ACCEPTS = frozenset({SubjectKind.DNI, SubjectKind.RUC})


def _open_page(
    session: Session,
    *,
    control: str | None,
    reset_cookies: bool,
    diagnostic_log: DiagnosticLog | None,
) -> EntelPage:
    page = EntelPage(
        session=session,
        control=control,
        reset_cookies=reset_cookies,
        diagnostic_log=diagnostic_log,
    )
    page.prepare()
    return page


def _row(subject: str, columns: dict[str, str], observed_at: str) -> list[str]:
    return [subject, columns["debt_total"], columns["has_punishment"], observed_at]


ENTEL = BrowserSite(
    name="entel",
    url=URL,
    export_header=("subject", "debt_total", "has_punishment", "observed_at"),
    accepts=lambda subject: subject.kind in _ACCEPTS,
    open_page=_open_page,
    row=_row,
)
