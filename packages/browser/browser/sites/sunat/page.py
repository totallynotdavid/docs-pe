from __future__ import annotations

import json
import time

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from browser.errors import BrowserError
from browser.result import LookupResult
from browser.sites.sunat.parse import (
    parse_identity,
    parse_razon_social,
    parse_representatives,
)
from browser.sites.sunat.request import (
    build_consulta_body,
    build_reps_body,
    random_token,
)


if TYPE_CHECKING:
    from browser.session import Session

URL = (
    "https://e-consultaruc.sunat.gob.pe/cl-ti-itmrconsruc/FrameCriterioBusquedaWeb.jsp"
)
SEARCH_BUTTON = "#btnAceptar"

# Cloudflare clears the browser once, on the search page. A synchronous request
# from that page reuses its clearance cookie, and SUNAT answers with the same
# HTML the search form would render.
POST_JS = """
(() => {
  const request = new XMLHttpRequest();
  request.open("POST", "jcrS00Alias", false);
  request.setRequestHeader("Content-Type", "application/x-www-form-urlencoded");
  request.send(%s);
  return JSON.stringify([request.status, request.responseText]);
})()
"""

# The challenge page loads before the search form while Cloudflare decides.
FORM_TIMEOUT_S = 60.0


class SunatPage(ABC):
    def __init__(self, *, session: Session) -> None:
        self._session = session

    def prepare(self, *, timeout_s: float = FORM_TIMEOUT_S) -> None:
        deadline = time.monotonic() + timeout_s

        while time.monotonic() < deadline:
            if self._session.is_element_present(SEARCH_BUTTON):
                return

            self._session.sleep(0.5)

        msg = "sunat search form did not render; Cloudflare challenge not cleared"
        raise BrowserError(msg)

    @abstractmethod
    def lookup(self, subject: str) -> LookupResult: ...

    def post(self, body: dict[str, str]) -> str:
        raw = self._session.evaluate(POST_JS % json.dumps(urlencode(body)))

        try:
            status, text = json.loads(raw)
        except (TypeError, ValueError) as exc:
            msg = "sunat request did not return a response"
            raise BrowserError(msg) from exc

        if status != 200:
            msg = f"sunat answered HTTP {status}"
            raise BrowserError(msg)

        return str(text)


class SunatIdentityPage(SunatPage):
    def lookup(self, subject: str) -> LookupResult:
        started = time.monotonic()
        page = self.post(build_consulta_body(ruc=subject, token=random_token()))
        identity = parse_identity(page)

        # An unregistered RUC is a final answer with no data row.
        columns = (
            {}
            if identity is None
            else {
                "tipo_doc": identity.tipo_doc,
                "num_doc": identity.num_doc,
                "nombre": identity.nombre,
                "tipo_contribuyente": identity.tipo_contribuyente,
            }
        )

        return LookupResult(
            subject=subject,
            columns=columns,
            elapsed_ms=_elapsed_ms(started),
            mint_ms=0,
        )


class SunatRepsPage(SunatPage):
    def lookup(self, subject: str) -> LookupResult:
        started = time.monotonic()
        razon_social = parse_razon_social(
            self.post(build_consulta_body(ruc=subject, token=random_token()))
        )

        if razon_social is None:
            return LookupResult(
                subject=subject,
                columns={},
                elapsed_ms=_elapsed_ms(started),
                mint_ms=0,
            )

        reps = parse_representatives(self.post(build_reps_body(ruc=subject)))

        return LookupResult(
            subject=subject,
            columns={
                "razon_social": razon_social,
                "representantes": json.dumps(
                    [
                        [
                            rep.doc_type,
                            rep.num_doc,
                            rep.nombre,
                            rep.cargo,
                            rep.fecha_desde,
                        ]
                        for rep in reps
                    ],
                    ensure_ascii=False,
                ),
            },
            elapsed_ms=_elapsed_ms(started),
            mint_ms=0,
        )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
