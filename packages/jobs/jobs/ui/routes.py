from __future__ import annotations

import os

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from jobs.ui.pages import (
    admin_page,
    dashboard_page,
    job_page,
    login_page,
    notifications_page,
    search_page,
)


if TYPE_CHECKING:
    from jobs.service import JobsService
    from jobs.settings import Settings


CurrentUser = Callable[[Request, "JobsService"], dict[str, Any]]
ReadUpload = Callable[[UploadFile], Awaitable[bytes]]
SetCookie = Callable[..., None]


def register_ui_routes(
    app: FastAPI,
    service: JobsService,
    settings: Settings,
    *,
    current_user: CurrentUser,
    read_upload: ReadUpload,
    set_session_cookie: SetCookie,
    set_csrf_cookie: SetCookie,
    csrf_cookie: str,
) -> None:
    @app.get("/login", response_class=HTMLResponse)
    def login_get(request: Request) -> str:
        return login_page(error=request.query_params.get("error", ""))

    @app.post("/login")
    async def login_post(
        email: str = Form(), password: str = Form()
    ) -> RedirectResponse:
        user = service.authenticate(email, password)
        if user is None:
            return RedirectResponse("/login?error=invalid", status_code=303)
        response = RedirectResponse("/", status_code=303)
        set_session_cookie(
            response,
            service.create_session(str(user["id"])),
            secure=settings.cookie_secure,
        )
        set_csrf_cookie(response, secure=settings.cookie_secure)
        return response

    @app.post("/logout")
    def logout_post(request: Request) -> RedirectResponse:
        session_id = request.cookies.get("jobs_session")
        if session_id:
            service.destroy_session(session_id)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("jobs_session", secure=settings.cookie_secure)
        response.delete_cookie(csrf_cookie, secure=settings.cookie_secure)
        return response

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> str:
        user = current_user(request, service)
        token = request.cookies.get(csrf_cookie, "")
        return dashboard_page(service, user, token) if user else ""

    @app.post("/ui/admin/team")
    async def create_team(request: Request, name: str = Form()) -> RedirectResponse:
        user = current_user(request, service)
        service.create_team(str(user["id"]), name=name)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/ui/admin/user")
    async def create_user(
        request: Request, email: str = Form(), password: str = Form()
    ) -> RedirectResponse:
        user = current_user(request, service)
        service.create_user(str(user["id"]), email=email, password=password)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/ui/admin/member")
    async def add_member(
        request: Request,
        team_id: str = Form(),
        user_id: str = Form(),
        role: str = Form(),
    ) -> RedirectResponse:
        user = current_user(request, service)
        service.add_membership(
            str(user["id"]), team_id=team_id, user_id=user_id, role=role
        )
        return RedirectResponse("/admin", status_code=303)

    @app.post("/ui/admin/remove-member")
    async def remove_member(
        request: Request, team_id: str = Form(), user_id: str = Form()
    ) -> RedirectResponse:
        user = current_user(request, service)
        service.remove_membership(str(user["id"]), team_id=team_id, user_id=user_id)
        return RedirectResponse("/admin", status_code=303)

    @app.post("/ui/admin/credential")
    async def store_credential(
        request: Request,
        team_id: str = Form(),
        provider: str = Form(),
        secret_ref: str = Form(),
        secret_json: str = Form(),
    ) -> RedirectResponse:
        user = current_user(request, service)
        service.store_team_credential(
            str(user["id"]),
            team_id=team_id,
            provider=provider,
            secret_ref=secret_ref,
            secret_json=secret_json,
        )
        return RedirectResponse("/admin", status_code=303)

    @app.post("/ui/jobs")
    async def submit_job(
        request: Request,
        team_id: str = Form(),
        sources: str = Form(),
        provider: str = Form(),
        input_file: UploadFile = File(),
    ) -> RedirectResponse:
        user = current_user(request, service)
        submitted = service.submit_job(
            str(user["id"]),
            team_id=team_id,
            sources=[value.strip() for value in sources.split(",")],
            provider=provider,
            input_bytes=await read_upload(input_file),
            idempotency_key=os.urandom(16).hex(),
        )
        return RedirectResponse(f"/jobs/{submitted.id}", status_code=303)

    @app.post("/ui/jobs/{job_id}/cancel")
    def cancel_job(request: Request, job_id: str) -> RedirectResponse:
        user = current_user(request, service)
        service.cancel_job(str(user["id"]), job_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/search", response_class=HTMLResponse)
    def search(
        request: Request,
        team_id: str = "",
        document: str = "",
        source: str = "",
        cursor: str | None = None,
        limit: int = 50,
    ) -> str:
        user = current_user(request, service)
        return search_page(
            service,
            user,
            team_id=team_id,
            document=document,
            source=source,
            cursor=cursor,
            limit=limit,
            csrf_token=request.cookies.get(csrf_cookie, ""),
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job(request: Request, job_id: str) -> str:
        user = current_user(request, service)
        return job_page(
            service,
            user,
            job_id=job_id,
            csrf_token=request.cookies.get(csrf_cookie, ""),
        )

    @app.get("/notifications", response_class=HTMLResponse)
    def notifications(request: Request, team_id: str = "") -> str:
        user = current_user(request, service)
        return notifications_page(
            service,
            user,
            team_id=team_id,
            csrf_token=request.cookies.get(csrf_cookie, ""),
        )

    @app.get("/admin", response_class=HTMLResponse)
    def admin(request: Request) -> str:
        user = current_user(request, service)
        if not user["is_site_admin"]:
            # Keep authorization in the service-backed UI boundary; this message is
            # only reachable for a logged-in non-admin and is not an API response.
            from jobs.service import PermissionDenied

            raise PermissionDenied("site administrator role is required")
        return admin_page(service, user, request.cookies.get(csrf_cookie, ""))
