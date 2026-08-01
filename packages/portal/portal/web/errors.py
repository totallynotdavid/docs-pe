from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import RedirectResponse

from portal.domain.errors import NotFound, PortalError
from portal.messages import message_for
from portal.web.render import render


if TYPE_CHECKING:
    from fastapi import FastAPI, Request, Response


class LoginRequired(Exception):
    """Raised by page dependencies when the browser has no usable session."""


def install_error_handlers(app: FastAPI) -> None:
    """Translate domain errors once, so routes only raise what they mean.

    A route that lets a `PortalError` escape is denying the request. A route that
    catches one is re-rendering its form with a message. This is also the only
    place a reason becomes Spanish, so nothing below the web boundary holds copy.
    """
    app.add_exception_handler(LoginRequired, _to_login)
    app.add_exception_handler(NotFound, _not_found)
    app.add_exception_handler(PortalError, _denied)


def _to_login(request: Request, error: Exception) -> Response:
    del request, error
    return RedirectResponse("/login", status_code=303)


def _not_found(request: Request, error: Exception) -> Response:
    return _problem(request, error, status_code=404)


def _denied(request: Request, error: Exception) -> Response:
    return _problem(request, error, status_code=403)


def _problem(request: Request, error: Exception, *, status_code: int) -> Response:
    """Answer in the same HTML shell the rest of the portal uses.

    This is a server-rendered app, so a refused action that returned raw JSON
    would drop the person out of the interface they were working in.
    """
    del request
    detail = message_for(error) if isinstance(error, PortalError) else ""
    response = render("Problem", detail=detail)
    response.status_code = status_code
    return response
