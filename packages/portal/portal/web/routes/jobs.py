from __future__ import annotations

import asyncio

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Annotated
from uuid import UUID

from litestar import Request, Response, Router, get, post
from litestar.datastructures import UploadFile
from litestar.di import NamedDependency
from litestar.enums import RequestEncodingType
from litestar.params import Body, FromPath, FromQuery
from litestar.response import Redirect
from litestar.response.sse import ServerSentEvent, ServerSentEventMessage
from litestar_htmx import HTMXRequest

from portal.application.service import PortalService
from portal.application.sessions import BrowserSessions
from portal.domain.errors import PortalError
from portal.domain.models import (
    TERMINAL_JOB_STATES,
    BrowserSession,
    JobItemCounts,
    SubmissionReview,
)
from portal.settings import PortalSettings
from portal.storage.port import ObjectStorage
from portal.web.deps import require_verified_session
from portal.web.render import render, render_fragment, render_hx
from portal.web.uploads import (
    MAX_CSV_UPLOAD_BYTES,
    MAX_CSV_UPLOAD_MB,
    MAX_REQUEST_BODY_BYTES,
    csv_input_lines,
    read_csv_upload,
)


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

SOURCE_NAMES = {option["id"]: option["name"] for option in SOURCE_OPTIONS}

PROGRESS_POLL_SECONDS = 0.5


async def _form_context(
    session: BrowserSession,
    service: PortalService,
    team_id: UUID,
    *,
    error: str,
) -> dict[str, object]:
    credentials = await service.credentials(session.user.id, team_id)
    active_credentials = tuple(
        credential for credential in credentials if credential.is_active
    )

    return {
        "user": session.user,
        "csrf_token": session.csrf_token,
        "team": await service.team(session.user.id, team_id),
        "credentials": active_credentials,
        "source_options": SOURCE_OPTIONS,
        "error": error,
        "max_upload_mb": MAX_CSV_UPLOAD_MB,
        "max_upload_bytes": MAX_CSV_UPLOAD_BYTES,
    }


def _review_by_source(
    review: SubmissionReview,
) -> tuple[dict[str, object], ...]:
    """Return per-source totals and reusable counts for the review screen."""
    reusable_pairs = {(item.document, item.source) for item in review.reusable}
    totals: dict[str, int] = {}
    reused: dict[str, int] = {}

    for item in review.items:
        totals[item.source] = totals.get(item.source, 0) + 1

        if (item.document, item.source) in reusable_pairs:
            reused[item.source] = reused.get(item.source, 0) + 1

    return tuple(
        {
            "source": source,
            "name": SOURCE_NAMES.get(source, source),
            "total": total,
            "reusable": reused.get(source, 0),
        }
        for source, total in totals.items()
    )


@get("/new")
async def new_job_get(
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
    team_id: FromPath[UUID],
) -> Response:
    context = await _form_context(page_session, service, team_id, error="")

    return render("JobForm", **context)


@dataclass
class JobSubmissionForm:
    credential_version_id: UUID
    csrf_token: str
    sources: list[str] = field(default_factory=list)
    filename: str = ""
    input_file: UploadFile | None = None


@post("", status_code=200, request_max_body_size=MAX_REQUEST_BODY_BYTES)
async def new_job_post(
    request: HTMXRequest,
    service: NamedDependency[PortalService],
    settings: NamedDependency[PortalSettings],
    storage: NamedDependency[ObjectStorage],
    team_id: FromPath[UUID],
    data: Annotated[
        JobSubmissionForm,
        Body(media_type=RequestEncodingType.MULTI_PART),
    ],
) -> Response:
    session = await require_verified_session(
        request,
        settings,
        data.csrf_token,
    )

    try:
        uploaded_filename, content = await read_csv_upload(data.input_file)
        filename = data.filename.strip() or uploaded_filename
        lines = csv_input_lines(content)
        sources = tuple(data.sources)

        reference = await service.store_upload(
            session.user.id,
            team_id,
            content=content,
            content_type="text/csv; charset=utf-8",
            storage=storage,
        )

        review = await service.preview_submission(
            session.user.id,
            team_id,
            lines,
            sources,
        )
    except (PortalError, ValueError, RuntimeError) as error:
        context = await _form_context(
            session,
            service,
            team_id,
            error=str(error),
        )

        return render("JobForm", **context)

    # A review is useful only when at least one item can be reused.
    if not review.reusable:
        job = await service.confirm_submission(
            actor_id=session.user.id,
            team_id=team_id,
            credential_version_id=data.credential_version_id,
            filename=filename,
            input_object_id=reference.id,
            lines=lines,
            sources=sources,
            reuse=True,
        )

        return Redirect(f"/teams/{team_id}/jobs/{job.id}", status_code=303)

    return render(
        "JobReview",
        user=session.user,
        csrf_token=session.csrf_token,
        team=await service.team(session.user.id, team_id),
        review=review,
        by_source=_review_by_source(review),
        filename=filename,
        sources=sources,
        input_object_id=reference.id,
        credential_version_id=data.credential_version_id,
    )


@post("/confirm", status_code=200)
async def confirm_job_post(
    request: HTMXRequest,
    service: NamedDependency[PortalService],
    settings: NamedDependency[PortalSettings],
    storage: NamedDependency[ObjectStorage],
    team_id: FromPath[UUID],
) -> Response:
    # Repeated form keys are required for msgspec to decode a list. Accept a
    # single selected source as well.
    form = await request.form()

    session = await require_verified_session(
        request,
        settings,
        str(form.get("csrf_token", "")),
    )

    # The upload is already durable. Do not trust a hidden field for the
    # document count or contents.
    reference = await service.input_reference(
        session.user.id,
        team_id,
        UUID(str(form.get("input_object_id", ""))),
    )
    content = await storage.open(reference)
    lines = csv_input_lines(content)

    job = await service.confirm_submission(
        actor_id=session.user.id,
        team_id=team_id,
        credential_version_id=UUID(str(form.get("credential_version_id", ""))),
        filename=str(form.get("filename", "")),
        input_object_id=reference.id,
        lines=lines,
        sources=tuple(str(value) for value in form.getall("sources")),
        reuse=str(form.get("reuse", "reuse")) == "reuse",
    )

    return Redirect(f"/teams/{team_id}/jobs/{job.id}", status_code=303)


@get("/{job_id:uuid}")
async def job_detail(
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
    team_id: FromPath[UUID],
    job_id: FromPath[UUID],
) -> Response:
    job = await service.job(page_session.user.id, team_id, job_id)
    counts = await service.job_progress_counts(page_session.user.id, team_id, job_id)
    items, total_items = await service.job_items(
        page_session.user.id,
        team_id,
        job_id,
        page=1,
    )

    return render(
        "JobDetail",
        user=page_session.user,
        csrf_token=page_session.csrf_token,
        team=await service.team(page_session.user.id, team_id),
        job=job,
        counts=counts,
        items=items,
        total_items=total_items,
        page=1,
    )


@get("/{job_id:uuid}/items")
async def job_items_page(
    request: HTMXRequest,
    page_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
    team_id: FromPath[UUID],
    job_id: FromPath[UUID],
    page: FromQuery[int] = 1,
) -> Response:
    current_page = max(page, 1)
    job = await service.job(page_session.user.id, team_id, job_id)
    counts = await service.job_progress_counts(page_session.user.id, team_id, job_id)
    items, total_items = await service.job_items(
        page_session.user.id,
        team_id,
        job_id,
        page=current_page,
    )

    # Full context even though a page-turn click is always an HTMX request:
    # render_hx falls back to the whole JobDetail page on a direct/non-HTMX
    # hit (a bookmarked or pasted items?page=2 URL), which needs everything
    # JobDetail itself requires, not just what the fragment uses.
    return render_hx(
        request,
        "JobDetail",
        "JobItemsFragment",
        user=page_session.user,
        csrf_token=page_session.csrf_token,
        team=await service.team(page_session.user.id, team_id),
        job=job,
        counts=counts,
        items=items,
        total_items=total_items,
        page=current_page,
    )


@dataclass
class CancelJobForm:
    csrf_token: str


@post("/{job_id:uuid}/cancel", status_code=200)
async def cancel_job(
    request: HTMXRequest,
    service: NamedDependency[PortalService],
    settings: NamedDependency[PortalSettings],
    team_id: FromPath[UUID],
    job_id: FromPath[UUID],
    data: Annotated[
        CancelJobForm,
        Body(media_type=RequestEncodingType.URL_ENCODED),
    ],
) -> Response:
    session = await require_verified_session(
        request,
        settings,
        data.csrf_token,
    )

    await service.cancel(session.user.id, team_id, job_id)

    return Redirect(f"/teams/{team_id}/jobs/{job_id}", status_code=303)


@get("/{job_id:uuid}/progress")
async def job_progress(
    request: Request,
    api_session: NamedDependency[BrowserSession],
    service: NamedDependency[PortalService],
    sessions: NamedDependency[BrowserSessions],
    settings: NamedDependency[PortalSettings],
    team_id: FromPath[UUID],
    job_id: FromPath[UUID],
) -> ServerSentEvent:
    # Authorize before opening the stream.
    await service.job(api_session.user.id, team_id, job_id)

    return ServerSentEvent(
        _progress_events(
            service,
            sessions,
            token=request.cookies.get(settings.session_cookie),
            actor_id=api_session.user.id,
            team_id=team_id,
            job_id=job_id,
            last_sequence=_last_event_id(request),
        )
    )


async def _progress_events(
    service: PortalService,
    sessions: BrowserSessions,
    *,
    token: str | None,
    actor_id: UUID,
    team_id: UUID,
    job_id: UUID,
    last_sequence: int,
) -> AsyncIterator[ServerSentEventMessage]:
    job = await service.job(actor_id, team_id, job_id)
    last_counts: JobItemCounts | None = None

    while True:
        # Re-checked on every poll: a stream must not outlive the session that
        # opened it, and the session can be destroyed from another tab.
        session = await sessions.load(token)

        if session is None or session.user.id != actor_id:
            return

        events = await service.job_events_after(
            actor_id,
            team_id,
            job_id,
            last_sequence,
        )

        for event in events:
            last_sequence = event.sequence
            job = await service.job(actor_id, team_id, job_id)
            last_counts = await service.job_progress_counts(actor_id, team_id, job_id)

            yield ServerSentEventMessage(
                id=event.sequence,
                event="progress",
                data=render_fragment("JobProgress", job=job, counts=last_counts),
            )

        if job.state in TERMINAL_JOB_STATES:
            yield ServerSentEventMessage(event="done", data="")
            return

        # Events cover job-level transitions. Poll state counts for item
        # progress between those transitions.
        counts = await service.job_progress_counts(actor_id, team_id, job_id)

        if counts != last_counts:
            last_counts = counts

            yield ServerSentEventMessage(
                id=last_sequence,
                event="progress",
                data=render_fragment("JobProgress", job=job, counts=counts),
            )

        await asyncio.sleep(PROGRESS_POLL_SECONDS)


def _last_event_id(request: Request) -> int:
    try:
        return max(int(request.headers.get("last-event-id", "0")), 0)
    except ValueError:
        return 0


router = Router(
    path="/teams/{team_id:uuid}/jobs",
    route_handlers=[
        new_job_get,
        new_job_post,
        confirm_job_post,
        job_detail,
        job_items_page,
        cancel_job,
        job_progress,
    ],
)
