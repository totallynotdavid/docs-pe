from __future__ import annotations

import asyncio
import os

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlparse
from uuid import UUID

from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from portal.application.service import PortalService
from portal.domain.errors import NotFound, PermissionDenied, PortalError
from portal.domain.models import JobState, TeamRole
from portal.migrations import apply_migrations
from portal.repository.memory import InMemoryPortalRepository
from portal.repository.postgres import PostgresPortalRepository
from portal.storage.memory import InMemoryObjectStorage, UnconfiguredObjectStorage
from portal.web.render import template_environment
from portal.web.security import same_origin


if TYPE_CHECKING:
    from portal.repository.protocols import PortalRepository
    from portal.storage.port import ObjectStorage


class ReadinessProbe(Protocol):
    """Small infrastructure boundary so readiness never depends on a UI route."""

    async def ready(self) -> bool: ...


@dataclass(frozen=True)
class PortalSettings:
    database_dsn: str
    environment: str = "development"
    public_origin: str = "http://testserver"
    cookie_secure: bool = False
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""

    @classmethod
    def from_environment(cls) -> PortalSettings:
        environment = os.environ.get("PORTAL_ENVIRONMENT", "development").lower()
        origin = os.environ.get("PORTAL_PUBLIC_ORIGIN", "")
        secure = os.environ.get("PORTAL_COOKIE_SECURE", "").lower()
        return cls(
            database_dsn=os.environ.get("PORTAL_DATABASE_DSN", ""),
            environment=environment,
            public_origin=origin
            or ("" if environment == "production" else "http://testserver"),
            cookie_secure=secure == "true" if secure else environment == "production",
            bootstrap_admin_email=os.environ.get("PORTAL_BOOTSTRAP_ADMIN_EMAIL", ""),
            bootstrap_admin_password=os.environ.get(
                "PORTAL_BOOTSTRAP_ADMIN_PASSWORD", ""
            ),
        )

    def validate(self) -> None:
        if self.environment != "production":
            return
        if not self.database_dsn:
            msg = "PORTAL_DATABASE_DSN es obligatorio en producción"
            raise RuntimeError(msg)
        if not self.cookie_secure or urlparse(self.public_origin).scheme != "https":
            msg = "producción requiere HTTPS y cookies Secure"
            raise RuntimeError(msg)
        if bool(self.bootstrap_admin_email) != bool(self.bootstrap_admin_password):
            msg = "el administrador inicial requiere correo y contraseña"
            raise RuntimeError(msg)

    @property
    def session_cookie(self) -> str:
        return "__Host-portal-id" if self.cookie_secure else "portal-id"


class DatabaseConfigured:
    """Foundation readiness probe; deployments can replace it with an asyncpg ping."""

    def __init__(self, settings: PortalSettings) -> None:
        self._settings = settings

    async def ready(self) -> bool:
        return bool(self._settings.database_dsn)


def create_app(
    settings: PortalSettings | None = None,
    readiness: ReadinessProbe | None = None,
    *,
    repository: PortalRepository | None = None,
    storage: ObjectStorage | None = None,
) -> FastAPI:
    """Create the server-rendered portal and inject adapters at its boundary."""
    settings = settings or PortalSettings.from_environment()
    settings.validate()
    readiness = readiness or DatabaseConfigured(settings)
    templates = template_environment()
    initial_repository = repository
    initial_storage = storage

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        active_repository = initial_repository
        pool = None
        if active_repository is None and settings.database_dsn:
            import asyncpg

            pool = await asyncpg.create_pool(settings.database_dsn)
            await apply_migrations(pool)
            active_repository = PostgresPortalRepository(pool)
        if active_repository is None:
            active_repository = InMemoryPortalRepository()
        app.state.service = PortalService(active_repository)
        app.state.storage = initial_storage or (
            UnconfiguredObjectStorage()
            if settings.environment == "production"
            else InMemoryObjectStorage()
        )
        if settings.bootstrap_admin_email:
            await app.state.service.bootstrap_site_admin(
                settings.bootstrap_admin_email, settings.bootstrap_admin_password
            )
        try:
            yield
        finally:
            if pool is not None:
                await pool.close()

    app = FastAPI(title="Worker", version="0.2.0", lifespan=lifespan)
    app.state.settings = settings
    app.mount(
        "/estatico",
        StaticFiles(directory=Path(__file__).with_name("static")),
        name="estatico",
    )
    if settings.environment == "production":
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=[urlparse(settings.public_origin).hostname or "localhost"],
        )
        app.add_middleware(HTTPSRedirectMiddleware)

    def render(
        name: str, *, request: Request | None = None, **context: object
    ) -> HTMLResponse:
        del request
        return HTMLResponse(templates.get_template(name).render(**context))

    def service(request: Request) -> PortalService:
        return request.app.state.service  # type: ignore[no-any-return]

    async def browser_session(request: Request):
        return await service(request).browser_session(
            request.cookies.get(settings.session_cookie)
        )

    async def page_user(request: Request):
        session = await browser_session(request)
        if session is None:
            return None, RedirectResponse("/login", status_code=303)
        return session, None

    def require_origin(request: Request) -> None:
        if not same_origin(
            origin=request.headers.get("origin"),
            referer=request.headers.get("referer"),
            trusted_origin=settings.public_origin,
        ):
            raise HTTPException(status_code=403, detail="origen no autorizado")

    async def mutation_user(request: Request, csrf_token: str | None):
        require_origin(request)
        try:
            return await service(request).verify_browser_csrf(
                request.cookies.get(settings.session_cookie), csrf_token
            )
        except PermissionDenied as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @app.get("/salud", tags=["operación"])
    async def health() -> dict[str, str]:
        return {"estado": "saludable"}

    @app.get("/listo", tags=["operación"])
    async def ready(response: Response) -> dict[str, str]:
        if not await readiness.ready():
            response.status_code = 503
            return {"estado": "no_listo"}
        return {"estado": "listo"}

    @app.get("/login", response_class=HTMLResponse)
    async def login_get(request: Request, error: str = "") -> Response:
        session = await browser_session(request)
        if session is not None:
            return RedirectResponse("/", status_code=303)
        csrf_token = await service(request).issue_login_csrf()
        return render("login.html", user=None, csrf_token=csrf_token, error=error)

    @app.post("/login")
    async def login_post(
        request: Request,
        email: str = Form(),
        password: str = Form(),
        csrf_token: str = Form(),
    ) -> Response:
        require_origin(request)
        if not await service(request).consume_login_csrf(csrf_token):
            raise HTTPException(status_code=403, detail="verificación CSRF no válida")
        client_ip = request.client.host if request.client else "desconocido"
        login = await service(request).login(email, password, client_ip)
        if login is None:
            return RedirectResponse("/login?error=1", status_code=303)
        _, token = login
        await service(request).destroy_session(
            request.cookies.get(settings.session_cookie)
        )
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            settings.session_cookie,
            token,
            path="/",
            secure=settings.cookie_secure,
            httponly=True,
            samesite="lax",
        )
        return response

    @app.post("/logout")
    async def logout_post(request: Request, csrf_token: str = Form()) -> Response:
        await mutation_user(request, csrf_token)
        await service(request).destroy_session(
            request.cookies.get(settings.session_cookie)
        )
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(settings.session_cookie, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> Response:
        session, redirect = await page_user(request)
        if redirect:
            return redirect
        return render(
            "dashboard.html",
            user=session.user,
            csrf_token=session.csrf_token,
            teams=await service(request).teams(session.user.id),
        )

    @app.get("/equipos/{team_id}", response_class=HTMLResponse)
    async def team_page(request: Request, team_id: UUID) -> Response:
        session, redirect = await page_user(request)
        if redirect:
            return redirect
        try:
            team = await service(request).team(session.user.id, team_id)
            jobs, total = await service(request).jobs(session.user.id, team_id, page=1)
        except PortalError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return render(
            "team.html",
            user=session.user,
            csrf_token=session.csrf_token,
            team=team,
            jobs=jobs,
            total=total,
            page=1,
        )

    @app.get("/equipos/{team_id}/procesos/fragmento", response_class=HTMLResponse)
    async def jobs_fragment(request: Request, team_id: UUID, page: int = 1) -> Response:
        session, redirect = await page_user(request)
        if redirect:
            return redirect
        try:
            team = await service(request).team(session.user.id, team_id)
            jobs, total = await service(request).jobs(
                session.user.id, team_id, page=max(page, 1)
            )
        except PortalError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return render(
            "fragments/jobs.html", team=team, jobs=jobs, total=total, page=max(page, 1)
        )

    @app.get("/equipos/{team_id}/procesos/nuevo", response_class=HTMLResponse)
    async def new_job_get(request: Request, team_id: UUID) -> Response:
        session, redirect = await page_user(request)
        if redirect:
            return redirect
        try:
            team = await service(request).team(session.user.id, team_id)
            credentials = await service(request).credentials(session.user.id, team_id)
        except PortalError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return render(
            "job_form.html",
            user=session.user,
            csrf_token=session.csrf_token,
            team=team,
            credentials=credentials,
            sources=("osiptel", "sunat", "sunat_reps"),
            error="",
        )

    @app.post("/equipos/{team_id}/procesos")
    async def new_job_post(
        request: Request,
        team_id: UUID,
        credential_version_id: UUID = Form(),
        filename: str = Form(),
        documents: str = Form(),
        sources: list[str] = Form(),
        csrf_token: str = Form(),
    ) -> Response:
        user = await mutation_user(request, csrf_token)
        try:
            job = await service(request).submit_text(
                actor_id=user.id,
                team_id=team_id,
                credential_version_id=credential_version_id,
                filename=filename,
                documents=documents,
                sources=tuple(sources),
                storage=request.app.state.storage,
            )
        except (PortalError, ValueError, RuntimeError) as error:
            session = await browser_session(request)
            if session is None:
                raise HTTPException(
                    status_code=403, detail="sesión no válida"
                ) from error
            team = await service(request).team(user.id, team_id)
            credentials = await service(request).credentials(user.id, team_id)
            return render(
                "job_form.html",
                user=user,
                csrf_token=session.csrf_token,
                team=team,
                credentials=credentials,
                sources=("osiptel", "sunat", "sunat_reps"),
                error=str(error),
            )
        return RedirectResponse(
            f"/equipos/{team_id}/procesos/{job.id}", status_code=303
        )

    @app.get("/equipos/{team_id}/procesos/{job_id}", response_class=HTMLResponse)
    async def job_detail(request: Request, team_id: UUID, job_id: UUID) -> Response:
        session, redirect = await page_user(request)
        if redirect:
            return redirect
        try:
            team = await service(request).team(session.user.id, team_id)
            job = await service(request).job(session.user.id, team_id, job_id)
        except NotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionDenied as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return render(
            "job_detail.html",
            user=session.user,
            csrf_token=session.csrf_token,
            team=team,
            job=job,
        )

    @app.post("/equipos/{team_id}/procesos/{job_id}/cancelar")
    async def cancel_job(
        request: Request, team_id: UUID, job_id: UUID, csrf_token: str = Form()
    ) -> Response:
        user = await mutation_user(request, csrf_token)
        try:
            await service(request).cancel(user.id, team_id, job_id)
        except NotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionDenied as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return RedirectResponse(
            f"/equipos/{team_id}/procesos/{job_id}", status_code=303
        )

    @app.get("/equipos/{team_id}/procesos/{job_id}/progreso")
    async def job_progress(request: Request, team_id: UUID, job_id: UUID) -> Response:
        session = await browser_session(request)
        if session is None:
            raise HTTPException(status_code=401, detail="autenticación requerida")
        try:
            await service(request).job(session.user.id, team_id, job_id)
        except NotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionDenied as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        try:
            sequence = max(int(request.headers.get("last-event-id", "0")), 0)
        except ValueError:
            sequence = 0
        token = request.cookies.get(settings.session_cookie)

        async def event_stream() -> AsyncIterator[str]:
            nonlocal sequence
            while True:
                current_session = await service(request).browser_session(token)
                if (
                    current_session is None
                    or current_session.user.id != session.user.id
                ):
                    return
                events = await service(request).job_events_after(
                    current_session.user.id, team_id, job_id, sequence
                )
                for event in events:
                    sequence = event.sequence
                    job = await service(request).job(
                        current_session.user.id, team_id, job_id
                    )
                    fragment = templates.get_template(
                        "fragments/job_progress.html"
                    ).render(job=job)
                    payload = fragment.replace("\n", "\ndata: ")
                    yield f"id: {event.sequence}\nevent: progreso\ndata: {payload}\n\n"
                    if job.state in {
                        JobState.COMPLETED,
                        JobState.FAILED,
                        JobState.CANCELLED,
                    }:
                        return
                if await request.is_disconnected():
                    return
                current_job = await service(request).job(
                    current_session.user.id, team_id, job_id
                )
                if current_job.state in {
                    JobState.COMPLETED,
                    JobState.FAILED,
                    JobState.CANCELLED,
                }:
                    return
                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/equipos/{team_id}/buscar", response_class=HTMLResponse)
    async def search_page(
        request: Request, team_id: UUID, q: str = "", page: int = 1
    ) -> Response:
        session, redirect = await page_user(request)
        if redirect:
            return redirect
        try:
            team = await service(request).team(session.user.id, team_id)
            results, has_more = await service(request).search(
                session.user.id, team_id, q, page=max(page, 1)
            )
        except PortalError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return render(
            "search.html",
            user=session.user,
            csrf_token=session.csrf_token,
            team=team,
            query=q,
            results=results,
            page=max(page, 1),
            has_more=has_more,
        )

    @app.get("/equipos/{team_id}/buscar/fragmento", response_class=HTMLResponse)
    async def search_fragment(
        request: Request, team_id: UUID, q: str = "", page: int = 1
    ) -> Response:
        session, redirect = await page_user(request)
        if redirect:
            return redirect
        try:
            team = await service(request).team(session.user.id, team_id)
            results, has_more = await service(request).search(
                session.user.id, team_id, q, page=max(page, 1)
            )
        except PortalError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return render(
            "fragments/search_results.html",
            team=team,
            query=q,
            results=results,
            page=max(page, 1),
            has_more=has_more,
        )

    @app.get("/notificaciones", response_class=HTMLResponse)
    async def notifications_page(request: Request) -> Response:
        session, redirect = await page_user(request)
        if redirect:
            return redirect
        notifications = await service(request).notifications(session.user.id)
        return render(
            "notifications.html",
            user=session.user,
            csrf_token=session.csrf_token,
            notifications=notifications,
        )

    @app.get("/notificaciones/fragmento", response_class=HTMLResponse)
    async def notifications_fragment(request: Request) -> Response:
        session, redirect = await page_user(request)
        if redirect:
            return redirect
        return render(
            "fragments/notifications.html",
            notifications=await service(request).notifications(session.user.id),
        )

    @app.get("/equipos/{team_id}/credenciales", response_class=HTMLResponse)
    async def credentials_get(request: Request, team_id: UUID) -> Response:
        session, redirect = await page_user(request)
        if redirect:
            return redirect
        try:
            team = await service(request).team(session.user.id, team_id)
            credentials = await service(request).credentials(session.user.id, team_id)
        except PortalError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return render(
            "credentials.html",
            user=session.user,
            csrf_token=session.csrf_token,
            team=team,
            credentials=credentials,
        )

    @app.post("/equipos/{team_id}/credenciales")
    async def credentials_post(
        request: Request,
        team_id: UUID,
        label: str = Form(),
        provider: str = Form(),
        key_id: str = Form(),
        config_ciphertext: str = Form(),
        csrf_token: str = Form(),
    ) -> Response:
        user = await mutation_user(request, csrf_token)
        try:
            await service(request).create_credential(
                user.id,
                team_id,
                label=label,
                provider=provider,
                key_id=key_id,
                config_ciphertext=config_ciphertext.encode("utf-8"),
            )
        except (PortalError, ValueError) as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return RedirectResponse(f"/equipos/{team_id}/credenciales", status_code=303)

    @app.get("/administracion", response_class=HTMLResponse)
    async def admin_page(request: Request) -> Response:
        session, redirect = await page_user(request)
        if redirect:
            return redirect
        try:
            await service(request).require_site_admin(session.user.id)
        except PermissionDenied as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return render(
            "admin.html",
            user=session.user,
            csrf_token=session.csrf_token,
            teams=await service(request).teams(session.user.id),
        )

    @app.post("/administracion/usuarios")
    async def admin_create_user(
        request: Request,
        email: str = Form(),
        password: str = Form(),
        csrf_token: str = Form(),
    ) -> Response:
        user = await mutation_user(request, csrf_token)
        try:
            await service(request).create_user(user.id, email, password)
        except (PermissionDenied, ValueError) as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return RedirectResponse("/administracion", status_code=303)

    @app.post("/administracion/equipos")
    async def admin_create_team(
        request: Request,
        slug: str = Form(),
        name: str = Form(),
        leader_id: UUID = Form(),
        csrf_token: str = Form(),
    ) -> Response:
        user = await mutation_user(request, csrf_token)
        try:
            await service(request).create_team(user.id, slug, name, leader_id)
        except (PermissionDenied, ValueError) as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return RedirectResponse("/administracion", status_code=303)

    @app.post("/administracion/equipos/{team_id}/miembros")
    async def admin_add_member(
        request: Request,
        team_id: UUID,
        user_id: UUID = Form(),
        role: TeamRole = Form(),
        csrf_token: str = Form(),
    ) -> Response:
        user = await mutation_user(request, csrf_token)
        try:
            await service(request).add_member(user.id, team_id, user_id, role)
        except (PermissionDenied, ValueError) as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return RedirectResponse("/administracion", status_code=303)

    @app.post("/administracion/miembros")
    async def admin_add_member_from_form(
        request: Request,
        team_id: UUID = Form(),
        user_id: UUID = Form(),
        role: TeamRole = Form(),
        csrf_token: str = Form(),
    ) -> Response:
        user = await mutation_user(request, csrf_token)
        try:
            await service(request).add_member(user.id, team_id, user_id, role)
        except (PermissionDenied, ValueError) as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        return RedirectResponse("/administracion", status_code=303)

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8000)
