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
        return _layout("OSIPTEL Jobs", _dashboard(service, user), user=user)

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
            f"<tr><td>{html.escape(item['source'])}</td><td>{html.escape(item['document'])}</td><td>{_status_badge(str(item['status']))}</td><td><code>{html.escape(json_string(item['payload']))}</code></td></tr>"
            for item in results
        )
        return _layout(
            "Published results",
            _search_workspace(team_id, document, source, rows),
            user=user,
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_page(request: Request, job_id: str) -> str:
        user = _current_user(request, service)
        job = service.job_view(str(user["id"]), job_id)
        body = _job_workspace(job_id, job)
        return _layout(f"Job {job_id}", body, user=user)

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


def _layout(title: str, body: str, *, user: dict[str, Any] | None = None) -> str:
    """Render a small, self-contained internal-tool shell.

    This stays server-rendered on purpose: the visual hierarchy is inspired by
    the CRM workspace while the jobs site keeps an entirely separate runtime,
    database, and authentication boundary.
    """
    page_title = html.escape(title)
    if user is None:
        return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'><title>{page_title} · OSIPTEL Jobs</title>
<style>{_styles()}</style></head><body class='auth-page'>
<main class='auth-shell'><a class='brand brand--auth' href='/'>
<span class='brand-mark' aria-hidden='true'><svg viewBox='0 0 24 24'><path d='M5 5.5h14v13H5zM8 9h8M8 12h5M8 15h8'/></svg></span>
<span><strong>OSIPTEL</strong><small>Jobs operations</small></span></a>
<section class='auth-card'><p class='eyebrow'>Secure workspace</p><h1>{page_title}</h1>{body}</section>
<p class='auth-footer'>Team-scoped data · durable job control · protected credentials</p></main>
</body></html>"""

    email = html.escape(str(user["email"]))
    initials = html.escape(
        "".join(part[:1] for part in email.split("@", 1)[0].split("."))[:2].upper()
        or "U"
    )
    admin_link = (
        "<a class='nav-item' href='#administration'><span aria-hidden='true'>⌘</span>Administration</a>"
        if user["is_site_admin"]
        else ""
    )
    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'><title>{page_title} · OSIPTEL Jobs</title>
<style>{_styles()}</style></head><body class='app-page'>
<div class='app-shell'><aside class='navigation-drawer'>
  <a class='brand' href='/'><span class='brand-mark' aria-hidden='true'><svg viewBox='0 0 24 24'><path d='M5 5.5h14v13H5zM8 9h8M8 12h5M8 15h8'/></svg></span><span><strong>OSIPTEL</strong><small>Jobs operations</small></span></a>
  <nav aria-label='Primary navigation' class='nav-list'>
    <a class='nav-item nav-item--active' href='/'><span aria-hidden='true'>▦</span>Workspace</a>
    <a class='nav-item' href='#jobs'><span aria-hidden='true'>◫</span>Team jobs</a>
    <a class='nav-item' href='#search'><span aria-hidden='true'>⌕</span>Published data</a>
    {admin_link}
  </nav>
  <div class='drawer-footer'><span class='environment-dot'></span><span>Control plane online</span></div>
</aside>
<main class='app-main'><header class='topbar'><div class='breadcrumb'><span>Operations</span><span aria-hidden='true'>/</span><strong>{page_title}</strong></div>
  <div class='account'><span class='avatar'>{initials}</span><span class='account-email'>{email}</span><form method='post' action='/logout'><button class='icon-button' aria-label='Sign out' title='Sign out' type='submit'>↗</button></form></div></header>
  <div class='page-content'>{body}</div>
</main></div></body></html>"""


def _login_form() -> str:
    return """<p class='lede'>Sign in with the local account provisioned for your team.</p>
<form class='stack-form' method='post' action='/login'>
  <label>Email address<input name='email' type='email' autocomplete='email' placeholder='name@company.com' required></label>
  <label>Password<input name='password' type='password' autocomplete='current-password' required></label>
  <button class='button button--primary' type='submit'>Sign in to workspace <span aria-hidden='true'>→</span></button>
</form><p class='helper-text'>The first site administrator is configured at deployment. There is no default account.</p>"""


def _dashboard(service: JobsService, user: dict[str, Any]) -> str:
    actor_id = str(user["id"])
    teams = service.list_teams(actor_id)
    team_options = "".join(
        f"<option value='{html.escape(team['id'])}'>{html.escape(team['name'])}</option>"
        for team in teams
    )
    jobs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for team in teams:
        jobs.extend((team, job) for job in service.list_jobs(actor_id, str(team["id"])))
    active_jobs = sum(job["state"] in {"running", "cancelling"} for _, job in jobs)
    queued_jobs = sum(job["state"] == "queued" for _, job in jobs)
    succeeded = sum(int(job["summary"]["succeeded"]) for _, job in jobs)
    remaining = sum(int(job["summary"]["remaining"]) for _, job in jobs)
    team_options = (
        team_options
        or "<option value='' disabled selected>Create a team first</option>"
    )

    admin = ""
    if user["is_site_admin"]:
        users = service.list_users(actor_id)
        user_options = "".join(
            f"<option value='{html.escape(item['id'])}'>{html.escape(item['email'])}</option>"
            for item in users
        )
        admin = f"""<details class='settings-panel' id='administration'><summary><span><span class='eyebrow'>Site administration</span><strong>Teams, people, and protected credentials</strong></span><span class='summary-action'>Manage <span aria-hidden='true'>⌄</span></span></summary>
<div class='admin-grid'>
  <section class='mini-panel'><h3>Create local user</h3><p>Provision a local sign-in for a member or leader.</p><form class='stack-form' method='post' action='/ui/admin/user'><label>Email<input name='email' type='email' required></label><label>Temporary password<input name='password' type='password' minlength='12' required></label><button class='button button--secondary'>Create user</button></form></section>
  <section class='mini-panel'><h3>Create team</h3><p>Teams are the access boundary for jobs and published data.</p><form class='stack-form' method='post' action='/ui/admin/team'><label>Team name<input name='name' required></label><button class='button button--secondary'>Create team</button></form></section>
  <section class='mini-panel'><h3>Membership</h3><p>Choose who can submit work or search the team's results.</p><form class='stack-form' method='post' action='/ui/admin/member'><label>Team<select name='team_id'>{team_options}</select></label><label>User<select name='user_id'>{user_options}</select></label><label>Role<select name='role'><option value='leader'>Team leader</option><option value='member'>Team member</option></select></label><button class='button button--secondary'>Save membership</button></form></section>
  <section class='mini-panel'><h3>Revoke membership</h3><p>Access is revoked immediately, including result search.</p><form class='stack-form' method='post' action='/ui/admin/remove-member'><label>Team<select name='team_id'>{team_options}</select></label><label>User<select name='user_id'>{user_options}</select></label><button class='button button--danger'>Revoke access</button></form></section>
  <section class='mini-panel mini-panel--wide'><h3>Team proxy credential</h3><p>Configuration is encrypted when saved and is never returned in this workspace.</p><form class='form-grid' method='post' action='/ui/admin/credential'><label>Team<select name='team_id'>{team_options}</select></label><label>Provider<select name='provider'><option value='geonode'>GeoNode</option><option value='dataimpulse'>DataImpulse</option></select></label><label>Secret reference<input name='secret_ref' placeholder='production/team-a/proxy' required></label><label class='field-span-2'>Credential configuration<textarea name='secret_json' placeholder='JSON configuration' required></textarea></label><div class='form-actions field-span-2'><button class='button button--secondary'>Encrypt and save credential</button></div></form></section>
</div></details>"""

    rows: list[str] = []
    for team, job in jobs:
        summary = job["summary"]
        rows.append(
            f"<tr><td>{html.escape(team['name'])}</td><td><a class='job-link' href='/jobs/{html.escape(job['id'])}'>{html.escape(job['id'])}</a></td><td>{_status_badge(str(job['state']))}</td><td>{summary['succeeded']}</td><td>{summary['remaining']}</td></tr>"
        )
    table_rows = (
        "".join(rows)
        or "<tr><td class='empty-cell' colspan='5'>No jobs yet. Submit a CSV when your team is ready.</td></tr>"
    )
    return f"""<section class='page-heading'><div><p class='eyebrow'>Batch lookup control</p><h1>Jobs workspace</h1><p class='lede'>Submit team-scoped lookup work, follow durable progress, and search only published results.</p></div><a class='button button--primary' href='#submit-job'>New job <span aria-hidden='true'>+</span></a></section>
<section class='metric-grid' aria-label='Workspace summary'>
  {_metric("Active jobs", str(active_jobs), "Currently held by workers")}
  {_metric("Queued", str(queued_jobs), "Waiting for global capacity")}
  {_metric("Published results", str(succeeded), "Canonical records retained")}
  {_metric("Rows remaining", str(remaining), f"Across {len(teams)} team" + ("" if len(teams) == 1 else "s"))}
</section>
<section class='work-grid'>
  <section class='panel' id='submit-job'><header class='panel-header'><div><p class='eyebrow'>New work</p><h2>Submit a lookup job</h2></div><span class='panel-icon' aria-hidden='true'>↑</span></header><p class='panel-copy'>Every physical CSV row is accepted or recorded as an explicit exclusion. Nothing disappears silently.</p>
  <form class='form-grid' method='post' action='/ui/jobs' enctype='multipart/form-data'><label>Team<select name='team_id'>{team_options}</select></label><label>Stable sources<input name='sources' value='osiptel' required><span class='field-hint'>Comma-separated: osiptel, sunat, sunat_reps</span></label><label>Proxy provider<select name='provider'><option value='geonode'>GeoNode</option><option value='dataimpulse'>DataImpulse</option></select></label><label>CSV input<input name='input_file' type='file' accept='.csv,text/csv' required></label><div class='form-actions field-span-2'><button class='button button--primary' type='submit'>Submit durable job <span aria-hidden='true'>→</span></button></div></form></section>
  <section class='panel' id='search'><header class='panel-header'><div><p class='eyebrow'>Published data</p><h2>Search your team's results</h2></div><span class='panel-icon' aria-hidden='true'>⌕</span></header><p class='panel-copy'>Membership is evaluated at read time. Removed members cannot search prior team data.</p>
  <form class='stack-form' method='get' action='/search'><label>Team<select name='team_id'>{team_options}</select></label><label>Document prefix<input name='document' placeholder='DNI or RUC'></label><label>Source<input name='source' placeholder='Optional: osiptel'></label><button class='button button--secondary' type='submit'>Search published data</button></form></section>
</section>
<section class='panel table-panel' id='jobs'><header class='panel-header'><div><p class='eyebrow'>Queue visibility</p><h2>Team jobs</h2></div><span class='panel-meta'>{len(jobs)} total</span></header><div class='table-wrap'><table><thead><tr><th>Team</th><th>Job</th><th>State</th><th>Succeeded</th><th>Remaining</th></tr></thead><tbody>{table_rows}</tbody></table></div></section>{admin}"""


def _search_workspace(team_id: str, document: str, source: str, rows: str) -> str:
    table_rows = (
        rows
        or "<tr><td class='empty-cell' colspan='4'>No published results match this search.</td></tr>"
    )
    return f"""<section class='page-heading page-heading--compact'><div><p class='eyebrow'>Published data</p><h1>Search results</h1><p class='lede'>Only canonical records published to this team are shown here.</p></div><a class='button button--secondary' href='/'>Back to workspace</a></section>
<section class='panel'><form class='search-bar' method='get' action='/search'><input type='hidden' name='team_id' value='{html.escape(team_id)}'><label>Document<input name='document' value='{html.escape(document)}' placeholder='DNI or RUC'></label><label>Source<input name='source' value='{html.escape(source)}' placeholder='Optional source'></label><button class='button button--primary'>Search</button></form></section>
<section class='panel table-panel'><header class='panel-header'><div><p class='eyebrow'>Results</p><h2>Published records</h2></div><span class='panel-meta'>Team scope active</span></header><div class='table-wrap'><table><thead><tr><th>Source</th><th>Document</th><th>Status</th><th>Data</th></tr></thead><tbody>{table_rows}</tbody></table></div></section>"""


def _job_workspace(job_id: str, job: dict[str, Any]) -> str:
    state = str(job["state"])
    summary_cards = "".join(
        _metric(key.replace("_", " ").title(), str(value), "Terminal summary")
        for key, value in job["summary"].items()
    )
    cancel = ""
    if state not in {"cancelled", "succeeded", "failed", "exhausted"}:
        cancel = f"<form method='post' action='/ui/jobs/{html.escape(job_id)}/cancel'><button class='button button--danger'>Cancel job</button></form>"
    return f"""<section class='page-heading page-heading--compact'><div><p class='eyebrow'>Job detail</p><h1>Job <span class='mono'>{html.escape(job_id)}</span></h1><p class='lede'>Server checkpoints are authoritative. Late worker writes are fenced after cancellation or lease changes.</p></div><div class='heading-actions'><a class='button button--secondary' href='/'>Back to workspace</a>{cancel}</div></section>
<section class='job-state'><div><span class='eyebrow'>Current state</span><div class='state-line'>{_status_badge(state)}<span>Live state from the durable control plane</span></div></div><span class='panel-icon' aria-hidden='true'>◫</span></section>
<section class='metric-grid metric-grid--summary'>{summary_cards}</section>"""


def _metric(label: str, value: str, detail: str) -> str:
    return f"<article class='metric-card'><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong><small>{html.escape(detail)}</small></article>"


def _status_badge(value: str) -> str:
    normalized = "".join(
        character
        for character in value.lower()
        if character.isalnum() or character == "-"
    )
    return f"<span class='status-badge status-badge--{html.escape(normalized)}'>{html.escape(value.replace('_', ' '))}</span>"


def _styles() -> str:
    return """
:root { color-scheme: light; --app:#f9f9f9; --surface:#fff; --ink:#333; --muted:#777; --faint:#979797; --line:#ebebeb; --line-strong:#d6d6d6; --hover:#f3f3f3; --dark:#333; --danger:#a93d3d; --success:#27804b; --warning:#9a6b15; --blue:#174fc5; --radius:10px; --shadow:0 1px 2px rgba(0,0,0,.025); font-family:Inter,"Segoe UI",Roboto,Arial,sans-serif; }
* { box-sizing:border-box; } body { margin:0; color:var(--ink); background:var(--app); font-size:13px; line-height:1.45; } button,input,select,textarea { font:inherit; } a { color:inherit; text-decoration:none; } h1,h2,h3,p { margin:0; } h1 { font-size:24px; line-height:1.2; letter-spacing:-.025em; font-weight:600; } h2 { font-size:15px; letter-spacing:-.01em; font-weight:600; } h3 { font-size:13px; font-weight:600; } .eyebrow { color:var(--faint); font-size:10px; font-weight:600; letter-spacing:.08em; text-transform:uppercase; } .lede { color:var(--muted); font-size:13px; line-height:1.55; margin-top:7px; max-width:640px; } .mono { font-family:"SFMono-Regular",Consolas,monospace; font-size:.62em; font-weight:500; letter-spacing:0; }
.app-shell { display:grid; grid-template-columns:220px minmax(0,1fr); min-height:100vh; } .navigation-drawer { background:#fff; border-right:1px solid var(--line); display:flex; flex-direction:column; gap:28px; padding:12px 12px 16px; } .brand { align-items:center; display:flex; gap:9px; min-height:32px; padding:2px 4px; } .brand strong { display:block; font-size:12px; letter-spacing:.065em; } .brand small { color:var(--muted); display:block; font-size:10px; margin-top:-1px; } .brand-mark { align-items:center; background:#333; border-radius:7px; color:#fff; display:inline-flex; height:28px; justify-content:center; width:28px; } .brand-mark svg { fill:none; height:17px; stroke:currentColor; stroke-linecap:round; stroke-linejoin:round; stroke-width:1.7; width:17px; } .nav-list { display:grid; gap:2px; } .nav-item { align-items:center; border-radius:6px; color:#686868; display:flex; font-size:12px; font-weight:500; gap:10px; min-height:31px; padding:0 9px; } .nav-item span { color:#909090; font-size:16px; line-height:1; text-align:center; width:14px; } .nav-item:hover { background:var(--hover); color:var(--ink); } .nav-item--active { background:#f0f0f0; color:var(--ink); } .nav-item--active span { color:var(--ink); } .drawer-footer { align-items:center; color:var(--muted); display:flex; font-size:10px; gap:7px; margin-top:auto; padding:0 8px; } .environment-dot { background:var(--success); border-radius:100%; height:6px; width:6px; }
.app-main { min-width:0; } .topbar { align-items:center; background:rgba(255,255,255,.92); border-bottom:1px solid var(--line); display:flex; height:48px; justify-content:space-between; padding:0 24px; position:sticky; top:0; z-index:2; } .breadcrumb { align-items:center; color:var(--muted); display:flex; font-size:11px; gap:7px; } .breadcrumb strong { color:var(--ink); font-weight:500; } .account { align-items:center; display:flex; gap:8px; } .account form { margin:0; } .avatar { align-items:center; background:#ebe3f8; border-radius:50%; color:#493266; display:inline-flex; font-size:10px; font-weight:600; height:24px; justify-content:center; width:24px; } .account-email { color:#666; font-size:11px; } .icon-button { background:transparent; border:0; border-radius:5px; color:#777; cursor:pointer; font-size:15px; height:28px; width:28px; } .icon-button:hover { background:var(--hover); color:var(--ink); } .page-content { margin:0 auto; max-width:1440px; padding:34px 40px 56px; }
.page-heading { align-items:flex-end; display:flex; gap:20px; justify-content:space-between; margin-bottom:24px; } .page-heading--compact { align-items:center; } .heading-actions { align-items:center; display:flex; flex-wrap:wrap; gap:8px; } .metric-grid { display:grid; gap:12px; grid-template-columns:repeat(4,minmax(0,1fr)); margin-bottom:18px; } .metric-grid--summary { grid-template-columns:repeat(auto-fit,minmax(145px,1fr)); } .metric-card { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); box-shadow:var(--shadow); display:flex; flex-direction:column; min-height:102px; padding:14px; } .metric-card span { color:var(--muted); font-size:11px; } .metric-card strong { font-size:24px; font-weight:500; letter-spacing:-.035em; line-height:1.15; margin:6px 0 auto; } .metric-card small { color:var(--faint); font-size:10px; line-height:1.3; }
.work-grid { display:grid; gap:18px; grid-template-columns:minmax(0,1.35fr) minmax(280px,.85fr); margin-bottom:18px; } .panel,.job-state { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); box-shadow:var(--shadow); } .panel { padding:18px; } .panel-header { align-items:flex-start; display:flex; justify-content:space-between; margin-bottom:6px; } .panel-header h2 { margin-top:2px; } .panel-icon { align-items:center; background:#f4f4f4; border-radius:6px; color:#666; display:inline-flex; font-size:16px; height:28px; justify-content:center; width:28px; } .panel-copy { color:var(--muted); font-size:11px; line-height:1.55; margin:10px 0 15px; max-width:62ch; } .panel-meta { color:var(--muted); font-size:11px; padding-top:3px; }
form { margin:0; } label { color:#555; display:grid; font-size:11px; font-weight:500; gap:5px; } input,select,textarea { background:#fff; border:1px solid var(--line-strong); border-radius:5px; color:var(--ink); min-height:32px; outline:none; padding:6px 8px; transition:border-color .15s,box-shadow .15s; width:100%; } input[type=file] { background:#fafafa; font-size:11px; padding:5px; } textarea { min-height:84px; resize:vertical; } input:focus,select:focus,textarea:focus { border-color:#777; box-shadow:0 0 0 3px rgba(30,30,30,.08); } .form-grid { display:grid; gap:12px; grid-template-columns:repeat(2,minmax(0,1fr)); } .stack-form { display:grid; gap:11px; } .field-span-2 { grid-column:span 2; } .field-hint { color:var(--faint); font-size:10px; font-weight:400; } .form-actions { align-items:center; display:flex; gap:8px; margin-top:2px; }
.button { align-items:center; border:1px solid transparent; border-radius:5px; cursor:pointer; display:inline-flex; font-size:11px; font-weight:500; gap:7px; justify-content:center; min-height:32px; padding:0 11px; white-space:nowrap; } .button--primary { background:var(--dark); color:#fff; } .button--primary:hover { background:#161616; } .button--secondary { background:#fff; border-color:var(--line-strong); color:#444; } .button--secondary:hover { background:#f6f6f6; } .button--danger { background:#fff; border-color:#e5bcbc; color:var(--danger); } .button--danger:hover { background:#fff4f4; }
.table-panel { padding:0; overflow:hidden; } .table-panel .panel-header { padding:18px 18px 10px; } .table-wrap { overflow:auto; } table { border-collapse:collapse; min-width:600px; width:100%; } th { background:#fcfcfc; border-bottom:1px solid var(--line); color:#858585; font-size:10px; font-weight:600; letter-spacing:.045em; padding:9px 18px; text-align:left; text-transform:uppercase; } td { border-bottom:1px solid var(--line); color:#555; font-size:11px; padding:11px 18px; vertical-align:middle; } tr:last-child td { border-bottom:0; } tbody tr:hover td { background:#fdfdfd; } .job-link { color:#333; font-family:"SFMono-Regular",Consolas,monospace; font-size:10px; } .job-link:hover { color:var(--blue); text-decoration:underline; } .empty-cell { color:var(--faint); padding:28px 18px; text-align:center; } .status-badge { border-radius:999px; display:inline-flex; font-size:10px; font-weight:600; line-height:1; padding:5px 8px; text-transform:capitalize; } .status-badge--running { background:#e8f0ff; color:#2557ab; } .status-badge--queued { background:#f2f2f2; color:#666; } .status-badge--cancelling,.status-badge--cancelled { background:#fceaea; color:#a33d3d; } .status-badge--succeeded { background:#e6f5eb; color:#287247; } .status-badge--failed,.status-badge--exhausted { background:#f9eeee; color:#a44444; } .status-badge--not-found,.status-badge--excluded { background:#fff5de; color:#906815; }
.settings-panel { background:var(--surface); border:1px solid var(--line); border-radius:var(--radius); box-shadow:var(--shadow); margin-top:18px; overflow:hidden; } .settings-panel summary { align-items:center; cursor:pointer; display:flex; justify-content:space-between; list-style:none; padding:16px 18px; } .settings-panel summary::-webkit-details-marker { display:none; } .settings-panel summary strong { display:block; font-size:13px; margin-top:2px; } .summary-action { color:var(--muted); font-size:11px; } .settings-panel[open] .summary-action span { display:inline-block; transform:rotate(180deg); } .admin-grid { border-top:1px solid var(--line); display:grid; gap:1px; grid-template-columns:repeat(2,minmax(0,1fr)); background:var(--line); } .mini-panel { background:#fff; padding:18px; } .mini-panel--wide { grid-column:span 2; } .mini-panel h3 { margin-bottom:3px; } .mini-panel p { color:var(--muted); font-size:11px; line-height:1.5; margin-bottom:13px; }
.search-bar { align-items:end; display:grid; gap:10px; grid-template-columns:minmax(160px,1fr) minmax(160px,1fr) auto; } .job-state { align-items:center; display:flex; justify-content:space-between; margin-bottom:18px; padding:18px; } .state-line { align-items:center; color:var(--muted); display:flex; font-size:11px; gap:9px; margin-top:6px; }
.auth-page { align-items:center; background:#f7f7f7; display:flex; justify-content:center; min-height:100vh; padding:24px; } .auth-shell { max-width:390px; width:100%; } .brand--auth { justify-content:center; margin-bottom:22px; } .auth-card { background:#fff; border:1px solid var(--line); border-radius:12px; box-shadow:0 12px 40px rgba(0,0,0,.045); padding:28px; } .auth-card h1 { margin-top:4px; } .auth-card .stack-form { margin-top:22px; } .auth-card .button { margin-top:4px; width:100%; } .helper-text { color:var(--faint); font-size:10px; line-height:1.5; margin-top:16px; } .auth-footer { color:#9a9a9a; font-size:10px; margin-top:18px; text-align:center; }
@media (max-width: 820px) { .app-shell { grid-template-columns:1fr; } .navigation-drawer { display:none; } .page-content { padding:24px 18px 40px; } .topbar { padding:0 18px; } .metric-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } .work-grid { grid-template-columns:1fr; } } @media (max-width: 520px) { .account-email { display:none; } .page-heading { align-items:flex-start; flex-direction:column; } .metric-grid { gap:8px; } .metric-card { min-height:92px; padding:12px; } .form-grid,.admin-grid { grid-template-columns:1fr; } .field-span-2,.mini-panel--wide { grid-column:auto; } .search-bar { grid-template-columns:1fr; } .search-bar .button { width:100%; } .heading-actions { width:100%; } .heading-actions > * { flex:1; } .state-line { align-items:flex-start; flex-direction:column; gap:5px; } }
"""


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
