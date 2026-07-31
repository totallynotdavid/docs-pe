from __future__ import annotations

import contextlib
import json
import time

from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from browser.errors import BrowserError, RejectedError
from browser.sites.entel.parse import parse_lookup_result
from browser.subject import Subject, SubjectKind


if TYPE_CHECKING:
    from browser.diagnostics import DiagnosticLog
    from browser.result import LookupResult
    from browser.session import Session


# The debt DataAction reads DocumentType as a plain string from the request
# body, so a template captured with any one kind serves both: we override
# DocumentType per lookup instead of re-driving the dropdown. These are the
# dropdown's own labels for the kinds Entel serves.
_DOCUMENT_TYPE: dict[SubjectKind, str] = {
    SubjectKind.RUC: "RUC",
    SubjectKind.DNI: "DNI",
}


URL = "https://miperfil.entel.pe/PE_Web_Cobro_Online_EU/"
# Warm-up identifier used to capture the request template and health-check the
# session when the run supplies no --control.
DEFAULT_CONTROL = "20610448187"
MIN_LOOKUP_INTERVAL_S = 0.5
ENDPOINT = (
    f"{URL}screenservices/PE_Web_Cobro_Online_CW/OnlinePayment/"
    "OnlinePayment_Step2/DataActionGetData"
)
# page-diagnostics.js lives at the package root, shared across sites.
PAGE_DIAGNOSTICS_JS = (
    Path(__file__).resolve().parents[2] / "page-diagnostics.js"
).read_text(encoding="utf-8")

BLOCK_STEP2_JS = r"""
(() => {
  window.__entelTemplate = null;
  if (window.__entelBlocking) return "already";
  window.__entelBlocking = true;
  const originalOpen = XMLHttpRequest.prototype.open;
  const originalSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) {
    this.__entelUrl = url;
    return originalOpen.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (body) {
    if (String(this.__entelUrl || "").includes("Step2/DataActionGetData")) {
      try { window.__entelTemplate = JSON.parse(body); } catch (error) {}
      return;
    }
    return originalSend.apply(this, arguments);
  };
  return "blocking";
})()
"""

INSTALL_LOOKUP_JS = r"""
(() => {
  window.__entelOutput = null;
  window.__entelDocument = null;
  window.__entelDocType = null;
  window.__entelLookup = async function (docType, documentNumber) {
    const started = performance.now();
    const tokenStarted = performance.now();
    const token = await grecaptcha.execute("0", {action: "SearchDebt"});
    const mintMs = Math.round(performance.now() - tokenStarted);
    const body = JSON.parse(JSON.stringify(window.__entelTemplate));
    body.screenData.variables.DocumentType = docType;
    body.screenData.variables.DocumentNumber = documentNumber;
    body.clientVariables.TokenCaptchaV3 = token;
    const match = decodeURIComponent(document.cookie).match(/crf=([^;]+)/);
    const response = await fetch(%s, {
      method: "POST",
      credentials: "include",
      body: JSON.stringify(body),
      headers: {
        accept: "application/json",
        "content-type": "application/json; charset=UTF-8",
        "x-csrftoken": match ? match[1] : "",
      },
    });
    const payload = await response.json();
    const data = payload.data || {};
    const resource = performance.getEntriesByName(%s).slice(-1)[0];
    const diagnostic = window.__entelCaptureDiagnostics
      ? await window.__entelCaptureDiagnostics("lookup", {
          document: documentNumber,
          transaction: {
            requestBodyBytes: JSON.stringify(body).length,
            csrfLength: match ? match[1].length : 0,
            tokenLength: token ? token.length : 0,
            mintMs,
            responseStatus: response.status,
            responseType: response.type,
            responseRedirected: response.redirected,
            serverHasError: data.HasErrorDebt,
            debtTotal: data.Debt ? data.Debt.DebtTotal : null,
            responseHeaders: Object.fromEntries(response.headers.entries()),
            resourceTiming: resource ? {
              duration: Math.round(resource.duration),
              transferSize: resource.transferSize,
              encodedBodySize: resource.encodedBodySize,
              decodedBodySize: resource.decodedBodySize,
              nextHopProtocol: resource.nextHopProtocol,
            } : null,
          },
        })
      : null;
    return {
      document: documentNumber,
      hasError: data.HasErrorDebt,
      debt: data.Debt || null,
      httpStatus: response.status,
      mintMs,
      tokenLength: token ? token.length : 0,
      elapsedMs: Math.round(performance.now() - started),
      diagnostic,
    };
  };
  let button = document.getElementById("entel-collector-go");
  if (!button) {
    button = document.createElement("button");
    button.id = "entel-collector-go";
    button.textContent = "GO";
    button.style = "position:fixed;top:60px;right:20px;z-index:2147483647;"
      + "padding:24px 32px;font-size:18px";
    button.onclick = async () => {
      window.__entelOutput = null;
      try {
        window.__entelOutput = await window.__entelLookup(
          window.__entelDocType, window.__entelDocument);
      } catch (error) {
        window.__entelOutput = {exception: String(error && error.message || error)};
      }
    };
    document.body.appendChild(button);
  }
  return "installed";
})()
"""

VISIBLE_INPUTS_JS = (
    "JSON.stringify([...document.querySelectorAll('input[type=text]')]"
    ".filter(element => element.offsetParent)"
    ".map(element => ({id: element.id, value: element.value})))"
)


class EntelPage:
    def __init__(
        self,
        *,
        session: Session,
        control: str | None,
        reset_cookies: bool = True,
        diagnostic_log: DiagnosticLog | None = None,
    ) -> None:
        self._session = session
        self._control = control or DEFAULT_CONTROL
        self._reset_cookies = reset_cookies
        self._diagnostic_log = diagnostic_log
        self._last_lookup_at = 0.0

    def prepare(self) -> None:
        self._wait_for_form()
        if self._reset_cookies:
            self._session.clear_cookies()
        self._session.goto(URL)
        self._wait_for_form()
        installed = self._session.evaluate(PAGE_DIAGNOSTICS_JS)
        if installed not in {"installed", "already-installed"}:
            msg = f"could not install Entel diagnostics: {installed!r}"
            raise BrowserError(msg)
        self._capture_stage("page-ready")
        result = self._session.evaluate(BLOCK_STEP2_JS)
        if result not in {"blocking", "already"}:
            msg = f"could not install Entel Step2 block: {result!r}"
            raise BrowserError(msg)
        self._drive_form(self._control)
        self._wait_for_template()
        self._capture_stage("template-captured")
        installed = self._session.evaluate(
            INSTALL_LOOKUP_JS % (json.dumps(ENDPOINT), json.dumps(ENDPOINT))
        )
        if installed != "installed":
            msg = f"could not install Entel lookup loop: {installed!r}"
            raise BrowserError(msg)
        # A structured reject proves the loop is functional (token minted,
        # request sent, JSON parsed). The reCAPTCHA v3 score fluctuates, so a
        # single control mint clears only about half the time; only a real
        # transport error here means the loop failed to install.
        with contextlib.suppress(RejectedError):
            self.lookup(self._control)

    def lookup(self, subject: str, *, timeout_s: float = 45.0) -> LookupResult:
        remaining = MIN_LOOKUP_INTERVAL_S - (time.monotonic() - self._last_lookup_at)
        if remaining > 0:
            time.sleep(remaining)
        doc_type = _DOCUMENT_TYPE[Subject(subject).kind]
        self._session.evaluate("window.__entelOutput = null")
        self._session.evaluate(f"window.__entelDocType = {json.dumps(doc_type)}")
        self._session.evaluate(f"window.__entelDocument = {json.dumps(subject)}")
        self._gui_click("#entel-collector-go")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            raw = self._session.evaluate("JSON.stringify(window.__entelOutput)")
            if raw and raw != "null":
                payload = json.loads(raw)
                self._last_lookup_at = time.monotonic()
                self._record_lookup_diagnostic(payload)
                return parse_lookup_result(payload, expected_document=subject)
            time.sleep(0.25)
        msg = f"Entel lookup timed out for document {subject}"
        raise BrowserError(msg)

    def check_health(self) -> bool:
        # The session is alive if the loop still returns a structured result.
        # A reject is a valid response (fluctuating v3 score), not ill health;
        # only transport, timeout, or WAF errors mean the session is dead.
        try:
            self.lookup(self._control)
        except RejectedError:
            return True
        except BrowserError:
            return False
        return True

    def _wait_for_template(self, *, timeout_s: float = 45.0) -> None:
        # Continuar kicks off intermediate OutSystems round trips before the
        # Step2 request is issued, so how long the template takes depends on
        # network latency (notably via a proxy exit). Poll instead of assuming.
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if self._session.evaluate("!!window.__entelTemplate") is True:
                return
            time.sleep(0.5)
        _fail("Entel form did not produce a Step2 request template")

    def _wait_for_form(self, *, timeout_s: float = 45.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            ready = self._session.evaluate(
                "!!document.querySelector('#b9-b1-Dropdown_DocumentType')"
            )
            if ready is True:
                return
            time.sleep(0.5)
        _fail("Entel form did not render")

    def _drive_form(self, document: str) -> None:
        # The form is driven once, with the RUC control, only to capture the
        # Step2 request template. That template serves every kind: lookup()
        # overrides DocumentType in the request body per call (RUC or DNI), so
        # the dropdown choice here is fixed and does not gate DNI lookups.
        for _ in range(3):
            self._gui_click("#b9-b1-Dropdown_DocumentType")
            time.sleep(1.2)
            tagged = self._session.evaluate(
                "(() => { const option = [...document.querySelectorAll("
                "'#b9-b1-Dropdown_DocumentType *')].find(element => "
                "element.children.length === 0 && element.innerText.trim() === 'RUC');"
                " if (!option) return false; option.id = 'entel-ruc-option';"
                " return true; })()"
            )
            if tagged is True:
                self._gui_click("#entel-ruc-option")
                time.sleep(1.2)
            selected = self._session.evaluate(
                "document.querySelector('#b9-b1-Dropdown_DocumentType')"
                ".innerText.trim().split('\\n').pop()"
            )
            if selected == "RUC":
                break
        else:
            _fail("could not select RUC document type")

        inputs = json.loads(self._session.evaluate(VISIBLE_INPUTS_JS))
        if not inputs:
            _fail("Entel RUC input was not found")
        selector = f"#{inputs[0]['id']}"
        self._gui_click(selector)
        time.sleep(0.4)
        self._session.gui_write(document)
        time.sleep(1.2)
        inputs = json.loads(self._session.evaluate(VISIBLE_INPUTS_JS))
        entered = inputs[0]["value"] if inputs else None
        if entered != document:
            msg = f"OS input did not reach Entel form: got {entered!r}"
            raise BrowserError(msg)

        for selector in ("#b9-b1-Checkbox1", "#b9-b1-Checkbox2"):
            self._gui_click(selector)
            time.sleep(0.4)
        button_state = self._session.evaluate(
            "(() => { const button = [...document.querySelectorAll('button')]"
            ".find(element => /Continuar/i.test(element.innerText));"
            " if (!button) return 'missing'; button.id = 'entel-continue';"
            " return String(button.disabled); })()"
        )
        if button_state != "false":
            msg = f"Entel Continuar button is not enabled: {button_state}"
            raise BrowserError(msg)
        self._gui_click("#entel-continue")
        time.sleep(3.0)

    def _gui_click(self, selector: str) -> None:
        self._session.evaluate(
            "(() => { const element = document.querySelector("
            + json.dumps(selector)
            + "); if (element) element.scrollIntoView({block: 'center'}); })()"
        )
        time.sleep(0.4)
        self._session.gui_click_element(selector)

    def _capture_stage(self, stage: str, *, timeout_s: float = 10.0) -> None:
        if self._diagnostic_log is None:
            return
        self._session.evaluate(
            "window.__entelDiagnosticOutput = null;"
            "window.__entelCaptureDiagnostics("
            + json.dumps(stage)
            + ").then(value => { window.__entelDiagnosticOutput = value; })"
            ".catch(error => { window.__entelDiagnosticOutput = "
            "{stage: "
            + json.dumps(stage)
            + ", exception: String(error && error.message || error)}; });"
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            raw = self._session.evaluate(
                "JSON.stringify(window.__entelDiagnosticOutput)"
            )
            if raw and raw != "null":
                event = json.loads(raw)
                if isinstance(event, dict):
                    self._diagnostic_log.record(event)
                return
            time.sleep(0.1)
        self._diagnostic_log.record(
            {"stage": stage, "exception": "diagnostic capture timed out"}
        )

    def _record_lookup_diagnostic(self, payload: object) -> None:
        if self._diagnostic_log is None or not isinstance(payload, dict):
            return
        event = payload.get("diagnostic")
        if isinstance(event, dict):
            self._diagnostic_log.record(event)


def _fail(message: str) -> NoReturn:
    raise BrowserError(message)
