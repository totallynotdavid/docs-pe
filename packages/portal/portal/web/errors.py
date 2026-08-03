from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import RedirectResponse

from portal.domain.errors import NotFound, PortalError
from portal.messages import message_for
from portal.web.render import render


if TYPE_CHECKING:
    from fastapi import FastAPI, Request, Response


class LoginRequired(Exception):
    pass


def install_error_handlers(app: FastAPI) -> None:
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
    del request

    detail = ""
    if isinstance(error, PortalError):
        detail = message_for(error)

    response = render("Problem", detail=detail)
    response.status_code = status_code
    return response
