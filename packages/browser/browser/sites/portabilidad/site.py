from __future__ import annotations

from typing import TYPE_CHECKING

from browser.sites.base import BrowserSite
from browser.sites.portabilidad.page import URL, PortabilidadPage
from browser.subject import SubjectKind


if TYPE_CHECKING:
    from browser.diagnostics import DiagnosticLog
    from browser.session import Session


def _open_page(
    session: Session,
    *,
    control: str | None,
    reset_cookies: bool,
    diagnostic_log: DiagnosticLog | None,
) -> PortabilidadPage:
    _ = control, reset_cookies

    page = PortabilidadPage(
        session=session,
        diagnostic_log=diagnostic_log,
    )
    page.prepare()

    return page


def _row(subject: str, columns: dict[str, str], observed_at: str) -> list[str]:
    return [
        subject,
        columns["receptor"],
        columns.get("cedente", ""),
        columns["asignatario_original"],
        columns.get("fecha_ventana", ""),
        columns["estado"],
        columns["current_carrier"],
        observed_at,
    ]


PORTABILIDAD = BrowserSite(
    name="portabilidad",
    url=URL,
    export_header=(
        "subject",
        "receptor",
        "cedente",
        "asignatario_original",
        "fecha_ventana",
        "estado",
        "current_carrier",
        "observed_at",
    ),
    accepts=lambda subject: subject.kind is SubjectKind.PHONE,
    open_page=_open_page,
    row=_row,
)
