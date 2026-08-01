from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse, RedirectResponse

from portal.domain.errors import NotFound, PortalError


if TYPE_CHECKING:
    from fastapi import FastAPI, Request, Response


class LoginRequired(Exception):
    """Raised by page dependencies when the browser has no usable session."""


def install_error_handlers(app: FastAPI) -> None:
    """Translate domain errors once, so routes only raise what they mean.

    A route that lets a `PortalError` escape is denying the request. A route
    that catches one is re-rendering its form with a message.
    """
    app.add_exception_handler(LoginRequired, _to_login)
    app.add_exception_handler(NotFound, _not_found)
    app.add_exception_handler(PortalError, _denied)


def _to_login(request: Request, error: Exception) -> Response:
    del request, error
    return RedirectResponse("/login", status_code=303)


def _not_found(request: Request, error: Exception) -> Response:
    del request
    return JSONResponse({"detail": str(error)}, status_code=404)


def _denied(request: Request, error: Exception) -> Response:
    del request
    return JSONResponse({"detail": str(error)}, status_code=403)
