from __future__ import annotations

import json
import os
import secrets

from collections.abc import Callable
from typing import Any

import uvicorn

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fetch.sites.registry import SITES

from jobs.service import (
    MAX_INPUT_BYTES,
    Cancelled,
    Conflict,
    JobsError,
    JobsService,
    NotFound,
    PermissionDenied,
)
from jobs.settings import Settings
from jobs.ui.routes import register_ui_routes


MAX_JSON_BODY_BYTES = 128 * 1024
CSRF_COOKIE = "jobs_csrf"


def create_app(settings: Settings) -> FastAPI:
    service = JobsService(settings)
    service.bootstrap_admin()
    app = FastAPI(title="Trabajos OSIPTEL", version="0.1.0")
    app.state.service = service

    @app.middleware("http")
    async def csrf_protection(
        request: Request, call_next: Callable[..., Any]
    ) -> Response:
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return await call_next(request)
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("multipart/form-data"):
            content_length = request.headers.get("content-length")
            if (
                content_length
                and content_length.isdigit()
                and int(content_length) > MAX_INPUT_BYTES + 128 * 1024
            ):
                return JSONResponse(
                    {"detail": "request body is too large"}, status_code=413
                )
        if request.cookies.get("jobs_session"):
            cookie_token = request.cookies.get(CSRF_COOKIE, "")
            supplied_token = request.headers.get("x-csrf-token", "")
            if not supplied_token and content_type.startswith(
                ("application/x-www-form-urlencoded", "multipart/form-data")
            ):
                form_limit = (
                    MAX_INPUT_BYTES + 128 * 1024
                    if content_type.startswith("multipart/form-data")
                    else MAX_JSON_BODY_BYTES
                )
                raw_form = await request.body()
                if len(raw_form) > form_limit:
                    return JSONResponse(
                        {"detail": "request body is too large"}, status_code=413
                    )
                form = await request.form()
                supplied_token = str(form.get("csrf_token", ""))
            if (
                not cookie_token
                or not supplied_token
                or not secrets.compare_digest(cookie_token, supplied_token)
            ):
                return JSONResponse(
                    {"detail": "CSRF validation failed"}, status_code=403
                )
        return await call_next(request)

    @app.exception_handler(JobsError)
    def jobs_error(request: Request, exc: JobsError) -> Response:
        status = (
            403
            if isinstance(exc, PermissionDenied)
            else 404
            if isinstance(exc, NotFound)
            else 409
        )
        if isinstance(exc, Cancelled):
            status = 409
        if isinstance(exc, PermissionDenied) and not request.url.path.startswith(
            "/api/"
        ):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": str(exc)}, status_code=status)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/sources")
    def sources() -> dict[str, Any]:
        return {
            "sources": [
                {"name": name, "columns": list(site.columns)}
                for name, site in sorted(SITES.items())
            ],
            "proxy_providers": ["geonode", "dataimpulse"],
        }

    @app.post("/api/auth/login")
    async def api_login(request: Request) -> JSONResponse:
        body = await _json_body(request)
        user = service.authenticate(
            str(body.get("email", "")), str(body.get("password", ""))
        )
        if user is None:
            return JSONResponse(
                {"detail": "invalid email or password"}, status_code=401
            )
        session_id = service.create_session(str(user["id"]))
        response = JSONResponse({"user": user})
        _set_session_cookie(response, session_id, secure=settings.cookie_secure)
        _set_csrf_cookie(response, secure=settings.cookie_secure)
        return response

    @app.post("/api/auth/logout")
    def api_logout(request: Request) -> Response:
        session_id = request.cookies.get("jobs_session")
        if session_id:
            service.destroy_session(session_id)
        response = Response(status_code=204)
        response.delete_cookie("jobs_session", secure=settings.cookie_secure)
        response.delete_cookie(CSRF_COOKIE, secure=settings.cookie_secure)
        return response

    @app.get("/api/me")
    def me(request: Request) -> dict[str, Any]:
        return _current_user(request, service)

    @app.get("/api/teams")
    def teams(request: Request) -> list[dict[str, Any]]:
        user = _current_user(request, service)
        return service.list_teams(str(user["id"]))

    @app.post("/api/admin/users")
    async def api_create_user(request: Request) -> dict[str, Any]:
        user = _current_user(request, service)
        body = await _json_body(request)
        return service.create_user(
            str(user["id"]),
            email=str(body.get("email", "")),
            password=str(body.get("password", "")),
            site_admin=bool(body.get("site_admin", False)),
        )

    @app.get("/api/admin/users")
    def api_users(request: Request) -> list[dict[str, Any]]:
        user = _current_user(request, service)
        return service.list_users(str(user["id"]))

    @app.post("/api/admin/teams")
    async def api_create_team(request: Request) -> dict[str, str]:
        user = _current_user(request, service)
        body = await _json_body(request)
        return service.create_team(str(user["id"]), name=str(body.get("name", "")))

    @app.get("/api/teams/{team_id}/members")
    def api_members(request: Request, team_id: str) -> list[dict[str, str]]:
        user = _current_user(request, service)
        return service.team_members(str(user["id"]), team_id)

    @app.put("/api/admin/teams/{team_id}/members/{user_id}")
    async def api_add_member(request: Request, team_id: str, user_id: str) -> Response:
        user = _current_user(request, service)
        body = await _json_body(request)
        service.add_membership(
            str(user["id"]),
            team_id=team_id,
            user_id=user_id,
            role=str(body.get("role", "")),
        )
        return Response(status_code=204)

    @app.delete("/api/admin/teams/{team_id}/members/{user_id}")
    def api_remove_member(request: Request, team_id: str, user_id: str) -> Response:
        user = _current_user(request, service)
        service.remove_membership(str(user["id"]), team_id=team_id, user_id=user_id)
        return Response(status_code=204)

    @app.put("/api/admin/teams/{team_id}/credentials/{provider}")
    async def api_credential(request: Request, team_id: str, provider: str) -> Response:
        user = _current_user(request, service)
        body = await _json_body(request)
        service.store_team_credential(
            str(user["id"]),
            team_id=team_id,
            provider=provider,
            secret_ref=str(body.get("secret_ref", "")),
            secret_json=json_string(body.get("secret", {})),
        )
        return Response(status_code=204)

    @app.get("/api/teams/{team_id}/credentials")
    def api_credential_metadata(request: Request, team_id: str) -> list[dict[str, str]]:
        user = _current_user(request, service)
        return service.credential_metadata(str(user["id"]), team_id)

    @app.post("/api/jobs")
    async def api_submit_job(
        request: Request,
        team_id: str = Form(),
        sources: str = Form(),
        provider: str = Form(),
        idempotency_key: str = Form(),
        input_file: UploadFile = File(),
    ) -> dict[str, Any]:
        user = _current_user(request, service)
        submitted = service.submit_job(
            str(user["id"]),
            team_id=team_id,
            sources=[value.strip() for value in sources.split(",")],
            provider=provider,
            input_bytes=await _read_upload(input_file),
            idempotency_key=idempotency_key,
        )
        return {"id": submitted.id, "reused": submitted.reused}

    @app.get("/api/teams/{team_id}/jobs")
    def api_jobs(request: Request, team_id: str) -> list[dict[str, Any]]:
        user = _current_user(request, service)
        return service.list_jobs(str(user["id"]), team_id)

    @app.get("/api/jobs/{job_id}")
    def api_job(request: Request, job_id: str) -> dict[str, Any]:
        user = _current_user(request, service)
        return service.job_view(str(user["id"]), job_id)

    @app.get("/api/jobs/{job_id}/exclusions")
    def api_exclusions(request: Request, job_id: str) -> list[dict[str, Any]]:
        user = _current_user(request, service)
        return service.job_exclusions(str(user["id"]), job_id)

    @app.post("/api/jobs/{job_id}/cancel")
    def api_cancel(request: Request, job_id: str) -> dict[str, Any]:
        user = _current_user(request, service)
        return service.cancel_job(str(user["id"]), job_id)

    @app.get("/api/teams/{team_id}/results")
    def api_results(
        request: Request,
        team_id: str,
        document: str = "",
        source: str = "",
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        user = _current_user(request, service)
        return service.search_results(
            str(user["id"]),
            team_id=team_id,
            document=document,
            source=source,
            cursor=cursor,
            limit=limit,
        )

    @app.post("/api/jobs/{job_id}/exports")
    def api_create_export(request: Request, job_id: str) -> dict[str, str]:
        user = _current_user(request, service)
        return service.create_export(str(user["id"]), job_id)

    @app.get("/api/exports/{export_id}")
    def api_download_export(request: Request, export_id: str) -> Response:
        user = _current_user(request, service)
        return Response(
            service.read_export(str(user["id"]), export_id),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{export_id}.csv"'},
        )

    @app.get("/api/teams/{team_id}/notifications")
    def api_notifications(request: Request, team_id: str) -> list[dict[str, str]]:
        user = _current_user(request, service)
        return service.notifications(str(user["id"]), team_id)

    # Worker routes use a separate bootstrap credential and never use browser sessions.
    @app.post("/api/worker/register")
    async def worker_register(request: Request) -> Response:
        body = await _json_body(request)
        service.register_worker(
            worker_id=str(body.get("worker_id", "")),
            bootstrap_token=str(body.get("token", "")),
            sources=[str(value) for value in body.get("sources", [])],
            capacity=int(body.get("capacity", 1)),
        )
        return Response(status_code=204)

    @app.post("/api/worker/claim")
    async def worker_claim(request: Request) -> list[dict[str, Any]]:
        body = await _json_body(request)
        return service.claim_work(
            worker_id=str(body.get("worker_id", "")),
            worker_token=str(body.get("token", "")),
            max_items=int(body.get("max_items", 1)),
            lease_seconds=int(body.get("lease_seconds", 60)),
        )

    @app.post("/api/worker/leases/{lease_id}/renew")
    async def worker_renew(request: Request, lease_id: str) -> dict[str, str]:
        body = await _json_body(request)
        return {
            "expires_at": service.renew_lease(
                worker_id=str(body.get("worker_id", "")),
                worker_token=str(body.get("token", "")),
                lease_id=lease_id,
                fence=int(body.get("fence", 0)),
                lease_seconds=int(body.get("lease_seconds", 60)),
            )
        }

    @app.post("/api/worker/leases/{lease_id}/cancelled")
    async def worker_cancelled(request: Request, lease_id: str) -> dict[str, bool]:
        body = await _json_body(request)
        return {
            "cancelled": service.lease_cancelled(
                worker_id=str(body.get("worker_id", "")),
                worker_token=str(body.get("token", "")),
                lease_id=lease_id,
            )
        }

    @app.post("/api/worker/leases/{lease_id}/credential")
    async def worker_credential(request: Request, lease_id: str) -> dict[str, Any]:
        body = await _json_body(request)
        return service.lease_credential(
            worker_id=str(body.get("worker_id", "")),
            worker_token=str(body.get("token", "")),
            lease_id=lease_id,
        )

    @app.post("/api/worker/checkpoints")
    async def worker_checkpoint(request: Request) -> dict[str, Any]:
        body = await _json_body(request)
        payload = body.get("payload")
        if payload is not None and not isinstance(payload, dict):
            raise Conflict("checkpoint payload must be an object")
        return service.checkpoint(
            worker_id=str(body.get("worker_id", "")),
            worker_token=str(body.get("token", "")),
            lease_id=str(body.get("lease_id", "")),
            work_item_id=str(body.get("work_item_id", "")),
            fence=int(body.get("fence", 0)),
            version=int(body.get("version", 0)),
            attempt_id=str(body.get("attempt_id", "")),
            sequence=int(body.get("sequence", 0)),
            outcome=str(body.get("outcome", "")),
            payload=payload,
            error_code=str(body.get("error_code", "")),
            healthy_contact_delta=int(body.get("healthy_contact_delta", 0)),
            retry_after_s=int(body.get("retry_after_s", 30)),
        )

    @app.post("/api/internal/sweep")
    def sweep(request: Request) -> dict[str, int]:
        user = _current_user(request, service)
        if not user["is_site_admin"]:
            raise PermissionDenied("site administrator role is required")
        return {"expired_leases": service.sweep_expired_leases()}

    register_ui_routes(
        app,
        service,
        settings,
        current_user=_current_user,
        read_upload=_read_upload,
        set_session_cookie=_set_session_cookie,
        set_csrf_cookie=_set_csrf_cookie,
        csrf_cookie=CSRF_COOKIE,
    )
    return app


def _current_user(request: Request, service: JobsService) -> dict[str, Any]:
    session_id = request.cookies.get("jobs_session")
    if not session_id:
        raise PermissionDenied("authentication required")
    user = service.session_user(session_id)
    if user is None:
        raise PermissionDenied("session has expired")
    return user


def _set_session_cookie(response: Response, session_id: str, *, secure: bool) -> None:
    response.set_cookie(
        "jobs_session",
        session_id,
        httponly=True,
        samesite="strict",
        secure=secure,
        max_age=43_200,
    )


def _set_csrf_cookie(response: Response, *, secure: bool) -> None:
    response.set_cookie(
        CSRF_COOKIE,
        secrets.token_urlsafe(32),
        httponly=False,
        samesite="strict",
        secure=secure,
        max_age=43_200,
    )


async def _json_body(request: Request) -> dict[str, Any]:
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_JSON_BODY_BYTES:
            raise Conflict("JSON request body is too large")
    try:
        body = json.loads(bytes(raw))
    except json.JSONDecodeError as exc:
        raise Conflict("request body must be valid JSON") from exc
    if not isinstance(body, dict):
        raise Conflict("request body must be a JSON object")
    return body


async def _read_upload(upload: UploadFile) -> bytes:
    content = await upload.read(MAX_INPUT_BYTES + 1)
    if len(content) > MAX_INPUT_BYTES:
        raise Conflict("input file is too large")
    return content


def json_string(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def main() -> None:
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings), host="0.0.0.0", port=int(os.environ.get("PORT", "8000"))
    )


app: Callable[[], FastAPI] | FastAPI
if os.environ.get("JOBS_SESSION_SECRET"):
    app = create_app(Settings.from_env())
else:
    # Console entrypoint calls main() and requires explicit deployment config.
    # Keeping import side-effect free lets tests construct an isolated app.
    app = FastAPI()
