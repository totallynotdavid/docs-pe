from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from portal.domain.models import Job, JobState, TeamRole


def template_environment() -> Environment:
    environment = Environment(
        loader=FileSystemLoader(Path(__file__).with_name("templates")),
        autoescape=select_autoescape(("html", "xml")),
    )
    environment.filters["estado"] = _state_label
    environment.filters["rol"] = _role_label
    environment.filters["notificacion"] = _notification_label
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
        TeamRole.SITE_ADMIN: "Administración del sitio",
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
