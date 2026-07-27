from __future__ import annotations

import html
import json

from collections.abc import Iterable
from typing import Any


STATUS_LABELS = {
    "queued": "En cola",
    "running": "En ejecución",
    "cancelling": "Cancelando",
    "cancelled": "Cancelado",
    "succeeded": "Completado",
    "failed": "Fallido",
    "exhausted": "Agotado",
    "not_found": "No encontrado",
    "excluded": "Excluido",
    "accepted": "Aceptado",
}

EXCLUSION_LABELS = {
    "invalid_document": "Documento inválido",
    "duplicate_document": "Documento duplicado",
    "not_supported_by_selected_sources": "No compatible con las fuentes seleccionadas",
}

EVENT_LABELS = {
    "job_queued": "El trabajo entró en cola",
    "job_running": "El trabajo empezó a ejecutarse",
    "job_succeeded": "El trabajo terminó correctamente",
    "job_cancelled": "El trabajo fue cancelado",
    "job_failed": "El trabajo terminó con errores",
    "job_exhausted": "El trabajo agotó sus intentos",
}


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def csrf_field(token: str) -> str:
    return f"<input type='hidden' name='csrf_token' value='{esc(token)}'>"


def status_badge(value: str) -> str:
    normalized = "".join(
        character
        for character in value.lower()
        if character.isalnum() or character == "-"
    )
    label = STATUS_LABELS.get(value, value.replace("_", " ").capitalize())
    return (
        f"<span class='status-badge status-badge--{esc(normalized)}' "
        f"aria-label='Estado: {esc(label)}'>{esc(label)}</span>"
    )


def metric_card(label: str, value: str, detail: str) -> str:
    return (
        "<article class='metric-card'>"
        f"<span>{esc(label)}</span><strong>{esc(value)}</strong>"
        f"<small>{esc(detail)}</small></article>"
    )


def flash_message(message: str, *, kind: str = "error") -> str:
    return f"<p class='flash flash--{esc(kind)}' role='alert'>{esc(message)}</p>"


def team_options(teams: Iterable[dict[str, Any]], *, selected: str = "") -> str:
    options = "".join(
        f"<option value='{esc(team['id'])}' {'selected' if str(team['id']) == selected else ''}>"
        f"{esc(team['name'])}</option>"
        for team in teams
    )
    return (
        options or "<option value='' disabled selected>Crea un equipo primero</option>"
    )


def result_rows(items: Iterable[dict[str, Any]]) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{esc(item['source'])}</td><td>{esc(item['document'])}</td>"
        f"<td>{status_badge(str(item['status']))}</td>"
        f"<td><code>{esc(json.dumps(item['payload'], ensure_ascii=False, separators=(',', ':')))}</code></td>"
        "</tr>"
        for item in items
    )
    return (
        rows
        or "<tr><td class='empty-cell' colspan='4'>No hay resultados publicados que coincidan con la búsqueda.</td></tr>"
    )


def exclusion_rows(exclusions: Iterable[dict[str, Any]]) -> str:
    rows = "".join(
        f"<tr><td>{esc(item['ordinal'])}</td><td>{esc(EXCLUSION_LABELS.get(str(item['reason']), str(item['reason'])))}</td></tr>"
        for item in exclusions
    )
    return (
        rows
        or "<tr><td class='empty-cell' colspan='2'>No hay filas excluidas.</td></tr>"
    )


def pagination(
    *, team_id: str, document: str, source: str, next_cursor: str | None
) -> str:
    if not next_cursor:
        return ""
    query = "&".join(
        f"{key}={html.escape(value, quote=True)}"
        for key, value in (
            ("team_id", team_id),
            ("document", document),
            ("source", source),
            ("cursor", next_cursor),
        )
        if value
    )
    return f"<nav class='pagination' aria-label='Paginación'><a class='button button--secondary' href='/search?{query}'>Siguiente <span aria-hidden='true'>→</span></a></nav>"


def progress_bar(summary: dict[str, Any]) -> str:
    terminal = sum(
        int(summary.get(key, 0))
        for key in (
            "succeeded",
            "not_found",
            "excluded",
            "exhausted_or_failed",
            "cancelled",
        )
    )
    total = terminal + int(summary.get("remaining", 0))
    percent = round(terminal / total * 100) if total else 0
    return (
        f"<div class='progress-copy'><span>Progreso</span><strong>{percent}%</strong></div>"
        f"<div class='progress-track' role='progressbar' aria-valuenow='{percent}' "
        f"aria-valuemin='0' aria-valuemax='100' aria-label='Progreso del trabajo'><span style='width:{percent}%'></span></div>"
    )
