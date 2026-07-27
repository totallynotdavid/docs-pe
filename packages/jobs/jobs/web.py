from __future__ import annotations

import html
import json
import os

from collections.abc import Callable
from typing import Any

import uvicorn

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fetch.sites.registry import SITES

from jobs.service import (
    Cancelled,
    Conflict,
    JobsError,
    JobsService,
    NotFound,
    PermissionDenied,
)
from jobs.settings import Settings


def create_app(settings: Settings) -> FastAPI:
    service = JobsService(settings)
    service.bootstrap_admin()
    app = FastAPI(title="OSIPTEL Jobs", version="0.1.0")
    app.state.service = service

    @app.exception_handler(JobsError)
    def jobs_error(_: Request, exc: JobsError) -> JSONResponse:
        status = (
            403
            if isinstance(exc, PermissionDenied)
            else 404
            if isinstance(exc, NotFound)
            else 409
        )
        if isinstance(exc, Cancelled):
            status = 409
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
        body = await request.json()
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
        return response

    @app.post("/api/auth/logout")
    def api_logout(request: Request) -> Response:
        session_id = request.cookies.get("jobs_session")
        if session_id:
            service.destroy_session(session_id)
        response = Response(status_code=204)
        response.delete_cookie("jobs_session")
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
        body = await request.json()
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
        body = await request.json()
        return service.create_team(str(user["id"]), name=str(body.get("name", "")))

    @app.get("/api/teams/{team_id}/members")
    def api_members(request: Request, team_id: str) -> list[dict[str, str]]:
        user = _current_user(request, service)
        return service.team_members(str(user["id"]), team_id)

    @app.put("/api/admin/teams/{team_id}/members/{user_id}")
    async def api_add_member(request: Request, team_id: str, user_id: str) -> Response:
        user = _current_user(request, service)
        body = await request.json()
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
        body = await request.json()
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
            input_bytes=await input_file.read(),
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
        request: Request, team_id: str, document: str = "", source: str = ""
    ) -> list[dict[str, Any]]:
        user = _current_user(request, service)
        return service.search_results(
            str(user["id"]), team_id=team_id, document=document, source=source
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
        body = await request.json()
        service.register_worker(
            worker_id=str(body.get("worker_id", "")),
            bootstrap_token=str(body.get("token", "")),
            sources=[str(value) for value in body.get("sources", [])],
            capacity=int(body.get("capacity", 1)),
        )
        return Response(status_code=204)

    @app.post("/api/worker/claim")
    async def worker_claim(request: Request) -> list[dict[str, Any]]:
        body = await request.json()
        return service.claim_work(
            worker_id=str(body.get("worker_id", "")),
            worker_token=str(body.get("token", "")),
            max_items=int(body.get("max_items", 1)),
            lease_seconds=int(body.get("lease_seconds", 60)),
        )

    @app.post("/api/worker/leases/{lease_id}/renew")
    async def worker_renew(request: Request, lease_id: str) -> dict[str, str]:
        body = await request.json()
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
        body = await request.json()
        return {
            "cancelled": service.lease_cancelled(
                worker_id=str(body.get("worker_id", "")),
                worker_token=str(body.get("token", "")),
                lease_id=lease_id,
            )
        }

    @app.post("/api/worker/leases/{lease_id}/credential")
    async def worker_credential(request: Request, lease_id: str) -> dict[str, Any]:
        body = await request.json()
        return service.lease_credential(
            worker_id=str(body.get("worker_id", "")),
            worker_token=str(body.get("token", "")),
            lease_id=lease_id,
        )

    @app.post("/api/worker/checkpoints")
    async def worker_checkpoint(request: Request) -> dict[str, Any]:
        body = await request.json()
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

    # Small server-rendered internal UI. It deliberately uses the same APIs and
    # leaves restricted uploads/diagnostics out of ordinary result views.
    @app.get("/login", response_class=HTMLResponse)
    def login_page() -> str:
        return _layout("Sign in", _login_form())

    @app.post("/login")
    async def login_form(
        request: Request, email: str = Form(), password: str = Form()
    ) -> RedirectResponse:
        user = service.authenticate(email, password)
        if user is None:
            return RedirectResponse("/login?error=invalid", status_code=303)
        response = RedirectResponse("/", status_code=303)
        _set_session_cookie(
            response,
            service.create_session(str(user["id"])),
            secure=settings.cookie_secure,
        )
        return response

    @app.post("/logout")
    def logout_form(request: Request) -> RedirectResponse:
        session_id = request.cookies.get("jobs_session")
        if session_id:
            service.destroy_session(session_id)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie("jobs_session")
        return response

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request) -> str:
        user = _current_user(request, service)
        return _layout("OSIPTEL Jobs", _dashboard(service, user))

    @app.post("/ui/admin/team")
    async def ui_create_team(request: Request, name: str = Form()) -> RedirectResponse:
        user = _current_user(request, service)
        service.create_team(str(user["id"]), name=name)
        return RedirectResponse("/", status_code=303)

    @app.post("/ui/admin/user")
    async def ui_create_user(
        request: Request, email: str = Form(), password: str = Form()
    ) -> RedirectResponse:
        user = _current_user(request, service)
        service.create_user(str(user["id"]), email=email, password=password)
        return RedirectResponse("/", status_code=303)

    @app.post("/ui/admin/member")
    async def ui_add_member(
        request: Request,
        team_id: str = Form(),
        user_id: str = Form(),
        role: str = Form(),
    ) -> RedirectResponse:
        user = _current_user(request, service)
        service.add_membership(
            str(user["id"]), team_id=team_id, user_id=user_id, role=role
        )
        return RedirectResponse("/", status_code=303)

    @app.post("/ui/admin/remove-member")
    async def ui_remove_member(
        request: Request, team_id: str = Form(), user_id: str = Form()
    ) -> RedirectResponse:
        user = _current_user(request, service)
        service.remove_membership(str(user["id"]), team_id=team_id, user_id=user_id)
        return RedirectResponse("/", status_code=303)

    @app.post("/ui/admin/credential")
    async def ui_store_credential(
        request: Request,
        team_id: str = Form(),
        provider: str = Form(),
        secret_ref: str = Form(),
        secret_json: str = Form(),
    ) -> RedirectResponse:
        user = _current_user(request, service)
        service.store_team_credential(
            str(user["id"]),
            team_id=team_id,
            provider=provider,
            secret_ref=secret_ref,
            secret_json=secret_json,
        )
        return RedirectResponse("/", status_code=303)

    @app.post("/ui/jobs")
    async def ui_submit(
        request: Request,
        team_id: str = Form(),
        sources: str = Form(),
        provider: str = Form(),
        input_file: UploadFile = File(),
    ) -> RedirectResponse:
        user = _current_user(request, service)
        submitted = service.submit_job(
            str(user["id"]),
            team_id=team_id,
            sources=[value.strip() for value in sources.split(",")],
            provider=provider,
            input_bytes=await input_file.read(),
            idempotency_key=os.urandom(16).hex(),
        )
        return RedirectResponse(f"/jobs/{submitted.id}", status_code=303)

    @app.post("/ui/jobs/{job_id}/cancel")
    def ui_cancel_job(request: Request, job_id: str) -> RedirectResponse:
        user = _current_user(request, service)
        service.cancel_job(str(user["id"]), job_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/search", response_class=HTMLResponse)
    def search_page(
        request: Request, team_id: str, document: str = "", source: str = ""
    ) -> str:
        user = _current_user(request, service)
        results = service.search_results(
            str(user["id"]), team_id=team_id, document=document, source=source
        )
        rows = "".join(
            f"<tr><td>{html.escape(item['source'])}</td><td>{html.escape(item['document'])}</td><td>{html.escape(item['status'])}</td><td><code>{html.escape(json_string(item['payload']))}</code></td></tr>"
            for item in results
        )
        return _layout(
            "Published results",
            "<table><thead><tr><th>Source</th><th>Document</th><th>Status</th><th>Data</th></tr></thead><tbody>"
            + rows
            + "</tbody></table><p><a href='/'>Back to jobs</a></p>",
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str) -> str:
        user = _current_user(request, service)
        job = service.job_view(str(user["id"]), job_id)
        summary = "".join(
            f"<li>{html.escape(key)}: {value}</li>"
            for key, value in job["summary"].items()
        )
        body = f"<p>State: <strong>{html.escape(job['state'])}</strong></p><ul>{summary}</ul><form method='post' action='/ui/jobs/{html.escape(job_id)}/cancel'><button>Cancel job</button></form><p><a href='/'>Back to jobs</a></p>"
        return _layout(f"Job {job_id}", body)

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


def json_string(value: object) -> str:
    return json.dumps(value, separators=(",", ":"))


def _layout(title: str, body: str) -> str:
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'><title>{html.escape(title)}</title>
<style>body{{font:15px system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem;color:#172033}}form{{border:1px solid #d8deea;padding:1rem;margin:1rem 0;display:grid;gap:.6rem}}input,select,textarea,button{{padding:.45rem}}button{{background:#1356a2;color:white;border:0;border-radius:4px}}table{{border-collapse:collapse;width:100%}}td,th{{border-bottom:1px solid #ddd;padding:.4rem;text-align:left}}.muted{{color:#5b6472}}</style></head><body><h1>{html.escape(title)}</h1>{body}</body></html>"""


def _login_form() -> str:
    return """<p class='muted'>Use a local account provisioned by the site administrator.</p><form method='post' action='/login'><label>Email <input name='email' type='email' required></label><label>Password <input name='password' type='password' required></label><button>Sign in</button></form><p class='muted'>The first administrator is created only from JOBS_BOOTSTRAP_ADMIN_EMAIL and JOBS_BOOTSTRAP_ADMIN_PASSWORD at deployment.</p>"""


def _dashboard(service: JobsService, user: dict[str, Any]) -> str:
    actor_id = str(user["id"])
    teams = service.list_teams(actor_id)
    team_options = "".join(
        f"<option value='{html.escape(team['id'])}'>{html.escape(team['name'])}</option>"
        for team in teams
    )
    sections = [
        f"<p>Signed in as {html.escape(str(user['email']))}. <form method='post' action='/logout'><button>Sign out</button></form></p>"
    ]
    if user["is_site_admin"]:
        users = service.list_users(actor_id)
        user_options = "".join(
            f"<option value='{html.escape(item['id'])}'>{html.escape(item['email'])}</option>"
            for item in users
        )
        sections.extend(
            [
                "<h2>Administration</h2><form method='post' action='/ui/admin/user'><label>Email <input name='email' type='email' required></label><label>Temporary password <input name='password' type='password' minlength='12' required></label><button>Create local user</button></form>",
                "<h2>Administration</h2><form method='post' action='/ui/admin/team'><label>New team <input name='name' required></label><button>Create team</button></form>",
                f"""<form method='post' action='/ui/admin/member'><label>Team <select name='team_id'>{team_options}</select></label><label>User <select name='user_id'>{user_options}</select></label><label>Role <select name='role'><option value='leader'>Team leader</option><option value='member'>Team member</option></select></label><button>Save membership</button></form>""",
                f"""<form method='post' action='/ui/admin/remove-member'><label>Team <select name='team_id'>{team_options}</select></label><label>User <select name='user_id'>{user_options}</select></label><button>Revoke team membership</button></form>""",
                f"""<form method='post' action='/ui/admin/credential'><label>Team <select name='team_id'>{team_options}</select></label><label>Provider <select name='provider'><option value='geonode'>GeoNode</option><option value='dataimpulse'>DataImpulse</option></select></label><label>Secret reference <input name='secret_ref' required></label><label>Credential configuration (encrypted on save) <textarea name='secret_json' required></textarea></label><button>Save proxy credential</button></form>""",
            ]
        )
    sections.extend(
        [
            "<h2>Submit a job</h2><p class='muted'>Input files are restricted; invalid, duplicate, and unsupported rows are recorded as exclusions.</p>",
            f"""<form method='post' action='/ui/jobs' enctype='multipart/form-data'><label>Team <select name='team_id'>{team_options}</select></label><label>Sources (comma-separated) <input name='sources' value='osiptel' required></label><label>Proxy provider <select name='provider'><option value='geonode'>GeoNode</option><option value='dataimpulse'>DataImpulse</option></select></label><label>CSV <input name='input_file' type='file' accept='.csv,text/csv' required></label><button>Submit durable job</button></form>""",
            f"""<form method='get' action='/search'><label>Team <select name='team_id'>{team_options}</select></label><label>Document prefix <input name='document'></label><label>Source <input name='source' placeholder='optional'></label><button>Search published data</button></form>""",
        ]
    )
    rows: list[str] = []
    for team in teams:
        for job in service.list_jobs(actor_id, str(team["id"])):
            summary = job["summary"]
            rows.append(
                f"<tr><td>{html.escape(team['name'])}</td><td><a href='/jobs/{html.escape(job['id'])}'>{html.escape(job['id'])}</a></td><td>{html.escape(job['state'])}</td><td>{summary['succeeded']}</td><td>{summary['remaining']}</td></tr>"
            )
    sections.append(
        "<h2>Team jobs</h2><table><thead><tr><th>Team</th><th>Job</th><th>State</th><th>Succeeded</th><th>Remaining</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )
    return "".join(sections)


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
