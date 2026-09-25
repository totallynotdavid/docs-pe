from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import quote

from litestar import Request
from litestar.exceptions.http_exceptions import RequestEntityTooLarge
from litestar.response import Redirect

from portal.domain.errors import (
    InputValidationError,
    NotFound,
    PermissionDenied,
    PortalError,
    Reason,
    StepUpRequired,
)
from portal.domain.models import AuditAction, AuditEvent
from portal.messages import message_for
from portal.web.render import render
from portal.web.trace import client_trace
from portal.web.uploads import MAX_CSV_UPLOAD_MB


if TYPE_CHECKING:
    from litestar import Response
    from litestar.types import ExceptionHandlersMap, Scope


class LoginRequired(Exception):
    pass


async def record_permission_denied(exception: Exception, scope: Scope) -> None:
    """This runs as an after_exception hook rather than inside the response
    handler because those are synchronous, and because a refusal is worth
    recording even when the response that follows is a plain 403 page.
    """
    if not isinstance(exception, PermissionDenied):
        return

    request: Request = Request(scope)

    await scope["app"].state.audit.record(
        AuditEvent(
            action=AuditAction.PERMISSION_DENIED,
            actor_id=request.state.get("actor_id"),
            trace=client_trace(request),
            metadata={"reason": exception.reason.value, "path": scope["path"]},
        )
    )


def _to_login(request: Request, error: Exception) -> Response:
    del request, error
    return Redirect("/login", status_code=303)


def _to_step_up(request: Request, error: Exception) -> Response:
    """Bounce to the reverify form, landing back on the page that asked.

    A POST that raised this loses its submission. After reverifying, the
    browser lands on a fresh GET, not a replay of the original form. That is
    acceptable because sensitive admin forms are short and the second-factor
    proof goes stale rarely.
    """
    del error
    return Redirect(
        f"/step-up?next_path={quote(request.url.path, safe='/')}", status_code=303
    )


def _not_found(request: Request, error: Exception) -> Response:
    return _problem(request, error, status_code=404)


def _denied(request: Request, error: Exception) -> Response:
    return _problem(request, error, status_code=403)


def _problem(request: Request, error: Exception, *, status_code: int) -> Response:
    del request

    detail = ""
    if isinstance(error, PortalError):
        detail = message_for(error)

    response = render("Problem", detail=detail)
    response.status_code = status_code
    return response


def _too_large(request: Request, error: Exception) -> Response:
    """The multipart body exceeded new_job_post's request_max_body_size.

    Litestar rejects the body while streaming it, before read_csv_upload runs,
    so no InputValidationError exists yet. Building one reuses the
    CSV_TOO_LARGE message.
    """
    del error
    return _problem(
        request,
        InputValidationError(Reason.CSV_TOO_LARGE, limit_mb=MAX_CSV_UPLOAD_MB),
        status_code=413,
    )


EXCEPTION_HANDLERS: ExceptionHandlersMap = {
    LoginRequired: _to_login,
    StepUpRequired: _to_step_up,
    NotFound: _not_found,
    PortalError: _denied,
    RequestEntityTooLarge: _too_large,
}

AFTER_EXCEPTION = (record_permission_denied,)
