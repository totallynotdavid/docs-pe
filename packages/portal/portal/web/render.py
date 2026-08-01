from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from portal.domain.models import CredentialState, Job, JobState, TeamRole


# Template names are positional-only so that a context key never shadows one.
def render_fragment(name: str, /, **context: object) -> str:
    """Render a template to markup, for embedding rather than responding."""
    return _TEMPLATES.get_template(name).render(**context)


def render(name: str, /, **context: object) -> HTMLResponse:
    """Render a template. Templates receive data, never the request."""
    return HTMLResponse(render_fragment(name, **context))


def render_hx(
    request: Request, page: str, fragment: str, /, **context: object
) -> HTMLResponse:
    """Serve one URL as a whole page, or as the fragment htmx swaps into it.

    `Vary` stops a cache from replaying a bare fragment into a navigation.
    """
    swap = request.headers.get("hx-request") == "true"
    response = render(fragment if swap else page, **context)
    response.headers["Vary"] = "HX-Request"
    return response


def template_environment() -> Environment:
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).with_name("templates")),
        autoescape=select_autoescape(("html", "xml")),
    )
    environment.filters["estado"] = _state_label
    environment.filters["rol"] = _role_label
    environment.filters["notificacion"] = _notification_label
    environment.filters["estado_credencial"] = _credential_state_label
    environment.globals["es_terminal"] = lambda job: (
        job.state
        in {
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    )
    environment.globals["resumen_proceso"] = _job_summary
    return environment


def _state_label(state: JobState) -> str:
    return {
        JobState.QUEUED: "En cola",
        JobState.RUNNING: "En ejecución",
        JobState.CANCELLING: "Cancelando",
        JobState.COMPLETED: "Completado",
        JobState.FAILED: "Con error",
        JobState.CANCELLED: "Cancelado",
    }[state]


def _role_label(role: TeamRole | None) -> str:
    return {
        TeamRole.TEAM_LEADER: "Liderazgo del equipo",
        TeamRole.TEAM_MEMBER: "Miembro del equipo",
        None: "",
    }[role]


def _job_summary(job: Job) -> str:
    if job.terminal_reason == "todos_los_registros_excluidos":
        return "La tarea terminó sin registros válidos."
    return f"Tarea {job.filename}: {_state_label(job.state)}."


def _notification_label(event_type: str) -> str:
    return {
        "proceso.completed": "Tarea completada",
        "proceso.failed": "Tarea con error",
        "proceso.cancelled": "Tarea cancelada",
    }.get(event_type, "Actualización de tarea")


def _credential_state_label(state: CredentialState) -> str:
    return {
        CredentialState.DRAFT: "Borrador",
        CredentialState.VALIDATING: "Validando",
        CredentialState.ACTIVE: "Activa",
        CredentialState.FAILED: "No validada",
        CredentialState.RETIRED: "Retirada",
    }[state]


_TEMPLATES = template_environment()
