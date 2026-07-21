from __future__ import annotations

import json
import secrets

from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

from capture.errors import CaptureError, RejectedError


if TYPE_CHECKING:
    from capture.diagnostics import DiagnosticLog
    from capture.ruc import RUC
    from capture.sites.base import CaptureSite
    from capture.store import ObservationStore


@dataclass
class RelayState:
    rucs: list[RUC]
    store: ObservationStore
    run_id: str
    token: str
    site: CaptureSite
    diagnostic_log: DiagnosticLog | None = None
    index: int = 0
    succeeded: int = 0
    rejected: int = 0
    failed: int = 0
    complete: bool = False

    @property
    def current_ruc(self) -> str | None:
        if self.index >= len(self.rucs):
            return None
        return str(self.rucs[self.index])

    def record(self, payload: object) -> None:
        ruc = self.current_ruc
        if ruc is None:
            msg = "the relay has no pending RUC"
            raise ValueError(msg)
        if not isinstance(payload, dict) or payload.get("ruc") != ruc:
            msg = "the browser returned an unexpected RUC"
            raise ValueError(msg)
        diagnostic = payload.get("diagnostic")
        if self.diagnostic_log is not None and isinstance(diagnostic, dict):
            self.diagnostic_log.record(diagnostic)

        try:
            result = self.site.parse(payload, expected_ruc=ruc)
        except RejectedError as exc:
            self.store.record_failure(
                run_id=self.run_id,
                site=self.site.name,
                ruc=ruc,
                status="rejected",
                error_detail=str(exc),
            )
            self.rejected += 1
            print(f"REJECTED {ruc}", flush=True)
        except CaptureError as exc:
            self.store.record_failure(
                run_id=self.run_id,
                site=self.site.name,
                ruc=ruc,
                status="failed",
                error_detail=str(exc),
            )
            self.failed += 1
            print(f"FAILED {ruc}: {exc}", flush=True)
        else:
            previous = self.store.latest(self.site.name, ruc)
            self.store.record_success(
                run_id=self.run_id,
                site=self.site.name,
                ruc=ruc,
                columns=result.columns,
            )
            changed = previous is not None and previous != result.columns
            marker = " CHANGED" if changed else ""
            print(
                f"OK {ruc} {_summary(result.columns)} ({result.elapsed_ms} ms){marker}",
                flush=True,
            )
            self.succeeded += 1
        self.index += 1


class CaptureRelayServer(HTTPServer):
    state: RelayState


class CaptureRelayHandler(BaseHTTPRequestHandler):
    server: CaptureRelayServer

    def do_OPTIONS(self) -> None:
        if not self._origin_allowed():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "Content-Type, X-Capture-Token"
        )
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()

    def do_GET(self) -> None:
        if self.path != "/next":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._authorized():
            return
        ruc = self.server.state.current_ruc
        self._json({"done": ruc is None, "ruc": ruc})
        if ruc is None:
            self.server.state.complete = True

    def do_POST(self) -> None:
        if self.path != "/result":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._authorized():
            return
        try:
            payload = self._read_payload()
            self.server.state.record(payload)
        except (ValueError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        self._json({"ok": True})

    def _read_payload(self) -> object:
        size = int(self.headers.get("Content-Length", "0"))
        if size < 1 or size > 1_000_000:
            msg = "invalid result size"
            raise ValueError(msg)
        return json.loads(self.rfile.read(size))

    def log_message(self, format_string: str, *args: Any) -> None:
        return

    def _authorized(self) -> bool:
        if not self._origin_allowed():
            self.send_error(HTTPStatus.FORBIDDEN)
            return False
        supplied = self.headers.get("X-Capture-Token", "")
        if not secrets.compare_digest(supplied, self.server.state.token):
            self.send_error(HTTPStatus.UNAUTHORIZED)
            return False
        return True

    def _origin_allowed(self) -> bool:
        return self.headers.get("Origin") == self.server.state.site.origin

    def _json(self, payload: object, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self._cors_headers()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", self.server.state.site.origin)
        self.send_header("Vary", "Origin")


def _summary(columns: dict[str, str]) -> str:
    return " ".join(f"{name}={value}" for name, value in columns.items())


def build_server(*, host: str, port: int, state: RelayState) -> CaptureRelayServer:
    server = CaptureRelayServer((host, port), CaptureRelayHandler)
    server.state = state
    return server


def write_browser_script(
    *, destination: Path, relay_url: str, token: str, site: CaptureSite
) -> None:
    diagnostics = (
        Path(__file__).with_name("page-diagnostics.js").read_text(encoding="utf-8")
    )
    template = site.script.read_text(encoding="utf-8")
    script = template.replace("__RELAY_URL__", json.dumps(relay_url)).replace(
        "__RELAY_TOKEN__", json.dumps(token)
    )
    destination.write_text(f"{diagnostics};\n{script}", encoding="utf-8")
