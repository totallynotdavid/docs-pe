from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jinja2 import Environment
from jinjax import Catalog
from litestar.enums import MediaType
from litestar.response import Response

from portal.branding import PRODUCT_MARK, PRODUCT_NAME
from portal.domain.models import CredentialState, Job, JobState, TeamRole
from portal.messages import choice_label, field_label, provider_label
from portal.security import totp_qr_svg
from portal.web.assets import COMPONENTS_DIR, PAGES_DIR, build_component_stylesheet


if TYPE_CHECKING:
    from litestar_htmx import HTMXRequest


def render_fragment(name: str, /, **context: Any) -> str:
    return _CATALOG.render(name, **context)


def render(name: str, /, **context: Any) -> Response[str]:
    return Response(render_fragment(name, **context), media_type=MediaType.HTML)


def render_hx(
    request: HTMXRequest,
    page: str,
    fragment: str,
    /,
    **context: Any,
) -> Response[str]:
    response = render(fragment if request.htmx else page, **context)

    # Prevent caches from serving an HTMX fragment as a full page.
    response.headers["Vary"] = "HX-Request"

    return response


def component_catalog() -> Catalog:
    environment = Environment(autoescape=True)

    environment.filters["job_state"] = _state_label
    environment.filters["role_name"] = _role_label
    environment.filters["notification"] = _notification_label
    environment.filters["credential_state"] = _credential_state_label
    environment.filters["exclusion_reason"] = _exclusion_reason_label

    environment.globals["is_terminal"] = _is_terminal
    environment.globals["job_summary"] = _job_summary
    environment.globals["field_label"] = field_label
    environment.globals["choice_label"] = choice_label
    environment.globals["provider_label"] = provider_label
    environment.globals["totp_qr_svg"] = totp_qr_svg
    environment.globals["component_stylesheet_url"] = build_component_stylesheet()
    environment.globals["PRODUCT_NAME"] = PRODUCT_NAME
    environment.globals["PRODUCT_MARK"] = PRODUCT_MARK

    catalog = Catalog(jinja_env=environment)
    catalog.add_folder(COMPONENTS_DIR)
    catalog.add_folder(PAGES_DIR)

    return catalog


def _is_terminal(job: Job) -> bool:
    return job.state in {
        JobState.COMPLETED,
        JobState.FAILED,
        JobState.CANCELLED,
    }


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
    if job.terminal_reason == "all_records_excluded":
        return "La tarea terminó sin registros válidos."

    return f"Tarea {job.filename}: {_state_label(job.state)}."


def _notification_label(event_type: str) -> str:
    return {
        "job.completed": "Tarea completada",
        "job.failed": "Tarea con error",
        "job.cancelled": "Tarea cancelada",
    }.get(event_type, "Actualización de tarea")


def _credential_state_label(state: CredentialState) -> str:
    return {
        CredentialState.DRAFT: "Borrador",
        CredentialState.VALIDATING: "Validando",
        CredentialState.ACTIVE: "Activa",
        CredentialState.FAILED: "No validada",
        CredentialState.RETIRED: "Retirada",
    }[state]


def _exclusion_reason_label(reason: str) -> str:
    return {
        "invalid_document": "documento no válido",
        "duplicate_document": "documento duplicado",
        "no_compatible_source": "ninguna fuente elegida lo acepta",
    }.get(reason, reason)


_CATALOG = component_catalog()
