from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from jinja2 import Environment
from jinjax import Catalog

from portal.domain.models import CredentialState, Job, JobState, TeamRole
from portal.messages import choice_label, field_label, provider_label


COMPONENTS_DIR = Path(__file__).with_name("components")
PAGES_DIR = Path(__file__).with_name("pages")

# Where a component's own stylesheet is served from. The catalog turns every
# `{#css UiPanel.css #}` into a link under this prefix.
COMPONENT_ASSETS_URL = "/estatico/componentes/"


# Component names are positional-only so that a context key never shadows one.
def render_fragment(name: str, /, **context: Any) -> str:
    """Render a component to markup, for embedding rather than responding."""
    return _CATALOG.render(name, **context)


def render(name: str, /, **context: Any) -> HTMLResponse:
    """Render a component. Components receive data, never the request."""
    return HTMLResponse(render_fragment(name, **context))


def render_hx(
    request: Request, page: str, fragment: str, /, **context: Any
) -> HTMLResponse:
    """Serve one URL as a whole page, or as the fragment htmx swaps into it.

    `Vary` stops a cache from replaying a bare fragment into a navigation.
    """
    swap = request.headers.get("hx-request") == "true"
    response = render(fragment if swap else page, **context)
    response.headers["Vary"] = "HX-Request"
    return response


def component_catalog() -> Catalog:
    """Build the catalog: `components/` are shared, `pages/` are the entry points.

    Both folders share one namespace, so a page names a component directly. The
    environment must be handed over pre-built: a catalog only inherits autoescaping
    from one it is given.
    """
    environment = Environment(autoescape=True)
    environment.filters["job_state"] = _state_label
    environment.filters["role_name"] = _role_label
    environment.filters["notification"] = _notification_label
    environment.filters["credential_state"] = _credential_state_label
    environment.globals["is_terminal"] = lambda job: (
        job.state
        in {
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    )
    environment.globals["job_summary"] = _job_summary
    # The schema is the engine's; only the wording is ours.
    environment.globals["field_label"] = field_label
    environment.globals["choice_label"] = choice_label
    environment.globals["provider_label"] = provider_label

    catalog = Catalog(jinja_env=environment, root_url=COMPONENT_ASSETS_URL)
    catalog.add_folder(COMPONENTS_DIR)
    catalog.add_folder(PAGES_DIR)
    return catalog


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


_CATALOG = component_catalog()
