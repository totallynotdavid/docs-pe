from __future__ import annotations

import asyncio

from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from portal.application.service import PortalService
from portal.domain.errors import PortalError
from portal.domain.models import TERMINAL_JOB_STATES, BrowserSession
from portal.web.deps import (
    ApiSession,
    PageSession,
    Service,
    Settings,
    Storage,
    VerifiedSession,
)
from portal.web.render import render, render_fragment
from portal.web.sse import sse_event
from portal.web.uploads import csv_input_lines, read_csv_upload


router = APIRouter(prefix="/equipos/{team_id}/procesos")

SOURCE_OPTIONS = (
    {
        "id": "sunat",
        "name": "DNI y nombre",
        "output": "DNI y nombre de la persona.",
        "eligibility": "Para RUC que empiezan en 10",
        "sample_headers": ("RUC", "DNI", "Nombre"),
        "sample_row": ("10412345678", "12345678", "María Pérez Gómez"),
        "default": True,
    },
    {
        "id": "osiptel",
        "name": "Líneas móviles",
        "output": "Modalidad, número oculto y operador de cada línea.",
        "eligibility": "Para DNI y RUC",
        "sample_headers": ("Documento", "Modalidad", "Número", "Operador"),
        "sample_row": ("10412345678", "Postpago", "98765••••", "CLARO"),
        "default": False,
    },
    {
        "id": "sunat_reps",
        "name": "Representantes legales",
        "output": "DNI, nombre y cargo de los representantes.",
        "eligibility": "Para RUC que empiezan en 20",
        "sample_headers": ("Razón social", "DNI", "Nombre", "Cargo"),
        "sample_row": (
            "Empresa S.A.C.",
            "12345678",
            "María Pérez Gómez",
            "Gerente",
        ),
        "default": False,
    },
)

PROGRESS_POLL_SECONDS = 0.5

# Tell the browser not to reconnect after the job finishes.
_STREAM_CLOSED = sse_event(event="fin", data="")


async def _form_context(
    session: BrowserSession,
    service: PortalService,
    team_id: UUID,
    *,
    error: str,
) -> dict[str, object]:
    credentials = await service.credentials(session.user.id, team_id)

    return {
        "user": session.user,
        "csrf_token": session.csrf_token,
        "team": await service.team(session.user.id, team_id),
        "credentials": tuple(
            credential for credential in credentials if credential.is_active
        ),
        "source_options": SOURCE_OPTIONS,
        "error": error,
    }


@router.get("/nuevo", response_class=HTMLResponse)
async def new_job_get(
    session: PageSession,
    service: Service,
    team_id: UUID,
) -> Response:
    context = await _form_context(session, service, team_id, error="")

    return render("JobForm", **context)


@router.post("")
async def new_job_post(
    session: VerifiedSession,
    service: Service,
    storage: Storage,
    team_id: UUID,
    credential_version_id: UUID = Form(),
    filename: str = Form(default=""),
    input_file: UploadFile | None = File(default=None),
    sources: list[str] = Form(),
) -> Response:
    try:
        uploaded_filename, content = await read_csv_upload(input_file)

        job = await service.submit_input(
            actor_id=session.user.id,
            team_id=team_id,
            credential_version_id=credential_version_id,
            filename=filename.strip() or uploaded_filename,
            content=content,
            content_type="text/csv; charset=utf-8",
            lines=csv_input_lines(content),
            sources=tuple(sources),
            storage=storage,
        )
    except (PortalError, ValueError, RuntimeError) as error:
        context = await _form_context(
            session,
            service,
            team_id,
            error=str(error),
        )

        return render("JobForm", **context)

    return RedirectResponse(
        f"/equipos/{team_id}/procesos/{job.id}",
        status_code=303,
    )


@router.get("/{job_id}", response_class=HTMLResponse)
async def job_detail(
    session: PageSession,
    service: Service,
    team_id: UUID,
    job_id: UUID,
) -> Response:
    return render(
        "JobDetail",
        user=session.user,
        csrf_token=session.csrf_token,
        team=await service.team(session.user.id, team_id),
        job=await service.job(session.user.id, team_id, job_id),
    )


@router.post("/{job_id}/cancelar")
async def cancel_job(
    session: VerifiedSession,
    service: Service,
    team_id: UUID,
    job_id: UUID,
) -> Response:
    await service.cancel(session.user.id, team_id, job_id)

    return RedirectResponse(
        f"/equipos/{team_id}/procesos/{job_id}",
        status_code=303,
    )


@router.get("/{job_id}/progreso")
async def job_progress(
    request: Request,
    session: ApiSession,
    service: Service,
    settings: Settings,
    team_id: UUID,
    job_id: UUID,
) -> Response:
    # Refuse unauthorized callers before opening the stream.
    await service.job(session.user.id, team_id, job_id)

    stream = _progress_stream(
        request,
        service,
        token=request.cookies.get(settings.session_cookie),
        actor_id=session.user.id,
        team_id=team_id,
        job_id=job_id,
        sequence=_last_event_id(request),
    )

    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


async def _progress_stream(
    request: Request,
    service: PortalService,
    *,
    token: str | None,
    actor_id: UUID,
    team_id: UUID,
    job_id: UUID,
    sequence: int,
) -> AsyncIterator[str]:
    while True:
        # End the stream if the session expires or changes.
        current_session = await service.browser_session(token)

        if current_session is None or current_session.user.id != actor_id:
            return

        events = await service.job_events_after(
            actor_id,
            team_id,
            job_id,
            sequence,
        )

        for event in events:
            sequence = event.sequence
            job = await service.job(actor_id, team_id, job_id)

            yield sse_event(
                event_id=event.sequence,
                event="progreso",
                data=render_fragment("JobProgressFragment", job=job),
            )

            if job.state in TERMINAL_JOB_STATES:
                yield _STREAM_CLOSED
                return

        if await request.is_disconnected():
            return

        job = await service.job(actor_id, team_id, job_id)

        if job.state in TERMINAL_JOB_STATES:
            yield _STREAM_CLOSED
            return

        await asyncio.sleep(PROGRESS_POLL_SECONDS)


def _last_event_id(request: Request) -> int:
    try:
        return max(int(request.headers.get("last-event-id", "0")), 0)
    except ValueError:
        return 0
