from __future__ import annotations

import json

from typing import TYPE_CHECKING

from browser.sites.base import BrowserSite
from browser.sites.sunat.page import (
    URL,
    SunatIdentityPage,
    SunatPage,
    SunatRepsPage,
)
from browser.subject import SubjectKind


if TYPE_CHECKING:
    from collections.abc import Callable

    from browser.diagnostics import DiagnosticLog
    from browser.session import Session
    from browser.subject import Subject


def _is_juridica(subject: Subject) -> bool:
    return subject.kind is SubjectKind.RUC and subject.startswith("20")


def _is_natural(subject: Subject) -> bool:
    return subject.kind is SubjectKind.RUC and not subject.startswith("20")


def _opener(page_class: type[SunatPage]) -> Callable[..., SunatPage]:
    def open_page(
        session: Session,
        *,
        control: str | None,
        reset_cookies: bool,
        diagnostic_log: DiagnosticLog | None,
    ) -> SunatPage:
        _ = control, reset_cookies, diagnostic_log
        page = page_class(session=session)
        page.prepare()
        return page

    return open_page


def _identity_rows(
    subject: str, columns: dict[str, str], observed_at: str
) -> list[list[str]]:
    if not columns:
        return []

    return [
        [
            subject,
            columns["tipo_doc"],
            columns["num_doc"],
            columns["nombre"],
            columns["tipo_contribuyente"],
            observed_at,
        ]
    ]


def _reps_rows(
    subject: str, columns: dict[str, str], observed_at: str
) -> list[list[str]]:
    if not columns:
        return []

    return [
        [subject, columns["razon_social"], *rep, observed_at]
        for rep in json.loads(columns["representantes"])
    ]


SUNAT = BrowserSite(
    name="sunat",
    url=URL,
    export_header=(
        "subject",
        "tipo_doc",
        "num_doc",
        "nombre",
        "tipo_contribuyente",
        "observed_at",
    ),
    accepts=_is_natural,
    open_page=_opener(SunatIdentityPage),
    rows=_identity_rows,
)

SUNAT_REPS = BrowserSite(
    name="sunat_reps",
    url=URL,
    export_header=(
        "subject",
        "razon_social",
        "doc_type",
        "num_doc",
        "nombre",
        "cargo",
        "fecha_desde",
        "observed_at",
    ),
    accepts=_is_juridica,
    open_page=_opener(SunatRepsPage),
    rows=_reps_rows,
)
