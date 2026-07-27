from __future__ import annotations

from typing import TYPE_CHECKING, Any

from jobs.ui.components import (
    EVENT_LABELS,
    csrf_field,
    esc,
    exclusion_rows,
    flash_message,
    metric_card,
    pagination,
    progress_bar,
    result_rows,
    status_badge,
    team_options,
)
from jobs.ui.layout import layout


if TYPE_CHECKING:
    from jobs.service import JobsService


def login_page(*, error: str = "") -> str:
    message = (
        flash_message("El correo o la contraseña no son correctos.") if error else ""
    )
    body = f"""<p class='lede'>Ingresa con la cuenta local configurada para tu equipo.</p>{message}
<form class='stack-form' method='post' action='/login'>
  <label>Correo electrónico<input name='email' type='email' autocomplete='email' placeholder='nombre@empresa.com' required></label>
  <label>Contraseña<input name='password' type='password' autocomplete='current-password' required></label>
  <button class='button button--primary' type='submit'>Iniciar sesión <span aria-hidden='true'>→</span></button>
</form><p class='helper-text'>La primera cuenta administradora se configura durante el despliegue. No existe una cuenta predeterminada.</p>"""
    return layout("Iniciar sesión", body)


def dashboard_page(service: JobsService, user: dict[str, Any], csrf_token: str) -> str:
    actor_id = str(user["id"])
    csrf = csrf_field(csrf_token)
    teams = service.list_teams(actor_id)
    options = team_options(teams)
    jobs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for team in teams:
        jobs.extend((team, job) for job in service.list_jobs(actor_id, str(team["id"])))
    active_jobs = sum(job["state"] in {"running", "cancelling"} for _, job in jobs)
    queued_jobs = sum(job["state"] == "queued" for _, job in jobs)
    succeeded = sum(int(job["summary"]["succeeded"]) for _, job in jobs)
    remaining = sum(int(job["summary"]["remaining"]) for _, job in jobs)
    rows = "".join(
        f"<tr><td>{esc(team['name'])}</td><td><a class='job-link' href='/jobs/{esc(job['id'])}'>{esc(job['id'])}</a></td>"
        f"<td>{status_badge(str(job['state']))}</td><td>{job['summary']['succeeded']}</td>"
        f"<td>{job['summary']['remaining']}</td></tr>"
        for team, job in jobs
    )
    table_rows = (
        rows
        or "<tr><td class='empty-cell' colspan='5'>Todavía no hay trabajos. Carga un CSV cuando tu equipo esté listo.</td></tr>"
    )
    admin_teaser = ""
    if user["is_site_admin"]:
        admin_teaser = """<details class='settings-panel' id='administration'><summary><span><span class='eyebrow'>Administración del sitio</span><strong>Equipos, personas y credenciales protegidas</strong></span><span class='summary-action'>Gestionar <span aria-hidden='true'>⌄</span></span></summary>
<div class='mini-panel'><p>Administra el acceso y la configuración desde un espacio separado.</p><a class='button button--secondary' href='/admin'>Abrir administración <span aria-hidden='true'>→</span></a></div></details>"""
    body = f"""<section class='page-heading'><div><p class='eyebrow'>Control de consultas por lote</p><h1>Panel de trabajos</h1><p class='lede'>Envía consultas por equipo, sigue su avance y busca únicamente resultados publicados.</p></div><a class='button button--primary' href='#submit-job'>Nuevo trabajo <span aria-hidden='true'>+</span></a></section>
<section class='metric-grid' aria-label='Resumen del panel'><div>{metric_card("Trabajos activos", str(active_jobs), "En manos de los procesos")}</div><div>{metric_card("En cola", str(queued_jobs), "Esperando capacidad disponible")}</div><div>{metric_card("Resultados publicados", str(succeeded), "Registros conservados")}</div><div>{metric_card("Filas pendientes", str(remaining), f"En {len(teams)} equipo" + ("" if len(teams) == 1 else "s"))}</div></section>
<section class='work-grid'><section class='panel' id='submit-job'><header class='panel-header'><div><p class='eyebrow'>Nuevo trabajo</p><h2>Enviar una consulta</h2></div><span class='panel-icon' aria-hidden='true'>↑</span></header><p class='panel-copy'>Cada fila del CSV se acepta o se registra como una exclusión explícita. Nada desaparece silenciosamente.</p>
<form class='form-grid' method='post' action='/ui/jobs' enctype='multipart/form-data'>{csrf}<label>Equipo<select name='team_id'>{options}</select></label><label>Fuentes disponibles<input name='sources' value='osiptel' required><span class='field-hint'>Separadas por comas: osiptel, sunat, sunat_reps</span></label><label>Proveedor proxy<select name='provider'><option value='geonode'>GeoNode</option><option value='dataimpulse'>DataImpulse</option></select></label><label>Archivo CSV<input name='input_file' type='file' accept='.csv,text/csv' required></label><div class='form-actions field-span-2'><button class='button button--primary' type='submit'>Crear trabajo <span aria-hidden='true'>→</span></button></div></form></section>
<section class='panel' id='search'><header class='panel-header'><div><p class='eyebrow'>Datos publicados</p><h2>Buscar resultados del equipo</h2></div><span class='panel-icon' aria-hidden='true'>⌕</span></header><p class='panel-copy'>La pertenencia se valida al consultar. Quien pierde acceso tampoco puede buscar datos anteriores.</p>
<form class='stack-form' method='get' action='/search'><label>Equipo<select name='team_id'>{options}</select></label><label>Inicio de documento<input name='document' placeholder='DNI o RUC'></label><label>Fuente<input name='source' placeholder='Opcional: osiptel'></label><button class='button button--secondary' type='submit'>Buscar resultados</button></form></section></section>
<section class='panel table-panel' id='jobs'><header class='panel-header'><div><p class='eyebrow'>Visibilidad de la cola</p><h2>Trabajos del equipo</h2></div><span class='panel-meta'>{len(jobs)} en total</span></header><div class='table-wrap'><table><thead><tr><th>Equipo</th><th>Trabajo</th><th>Estado</th><th>Completadas</th><th>Pendientes</th></tr></thead><tbody>{table_rows}</tbody></table></div></section>{admin_teaser}"""
    return layout(
        "Panel de trabajos",
        body,
        user=user,
        csrf_token=csrf_token,
        team_id=str(teams[0]["id"]) if teams else "",
    )


def search_page(
    service: JobsService,
    user: dict[str, Any],
    *,
    team_id: str,
    document: str,
    source: str,
    cursor: str | None,
    limit: int,
    csrf_token: str,
) -> str:
    teams = service.list_teams(str(user["id"]))
    if not team_id:
        body = f"""<section class='page-heading page-heading--compact'><div><p class='eyebrow'>Datos publicados</p><h1>Buscar resultados</h1><p class='lede'>Selecciona un equipo para consultar sus registros publicados.</p></div><a class='button button--secondary' href='/'>Volver al panel</a></section>
<section class='panel'><form class='stack-form' method='get' action='/search'><label>Equipo<select name='team_id' required>{team_options(teams)}</select></label><button class='button button--primary' type='submit'>Continuar</button></form></section>"""
        return layout(
            "Resultados", body, user=user, csrf_token=csrf_token, active="search"
        )
    result_page = service.search_results(
        str(user["id"]),
        team_id=team_id,
        document=document,
        source=source,
        cursor=cursor,
        limit=limit,
    )
    rows = result_rows(result_page["items"])
    body = f"""<section class='page-heading page-heading--compact'><div><p class='eyebrow'>Datos publicados</p><h1>Resultados</h1><p class='lede'>Aquí solo aparecen registros publicados para este equipo.</p></div><a class='button button--secondary' href='/'>Volver al panel</a></section>
<section class='panel'><form class='search-bar' method='get' action='/search'><input type='hidden' name='team_id' value='{esc(team_id)}'><label>Documento<input name='document' value='{esc(document)}' placeholder='DNI o RUC'></label><label>Fuente<input name='source' value='{esc(source)}' placeholder='Fuente opcional'></label><button class='button button--primary' type='submit'>Buscar</button></form></section>
<section class='panel table-panel'><header class='panel-header'><div><p class='eyebrow'>Resultados</p><h2>Registros publicados</h2></div><span class='panel-meta'>Equipo seleccionado</span></header><div class='table-wrap'><table><thead><tr><th>Fuente</th><th>Documento</th><th>Estado</th><th>Datos</th></tr></thead><tbody>{rows}</tbody></table></div></section>{pagination(team_id=team_id, document=document, source=source, next_cursor=result_page["next_cursor"])}"""
    return layout(
        "Resultados",
        body,
        user=user,
        csrf_token=csrf_token,
        active="search",
        team_id=team_id,
    )


def job_page(
    service: JobsService,
    user: dict[str, Any],
    *,
    job_id: str,
    csrf_token: str,
) -> str:
    job = service.job_view(str(user["id"]), job_id)
    state = str(job["state"])
    summary_labels = {
        "succeeded": "Completadas",
        "not_found": "No encontradas",
        "excluded": "Excluidas",
        "exhausted_or_failed": "Fallidas",
        "cancelled": "Canceladas",
        "remaining": "Pendientes",
    }
    summary_cards = "".join(
        metric_card(
            summary_labels[key], str(job["summary"].get(key, 0)), "Resumen actual"
        )
        for key in summary_labels
    )
    cancel = ""
    if state not in {"cancelled", "succeeded", "failed", "exhausted"}:
        cancel = f"<form method='post' action='/ui/jobs/{esc(job_id)}/cancel'>{csrf_field(csrf_token)}<button class='button button--danger' type='submit'>Cancelar trabajo</button></form>"
    exclusions = []
    if user["is_site_admin"] or _is_team_leader(service, user, str(job["team_id"])):
        exclusions = service.job_exclusions(str(user["id"]), job_id)
    exclusion_panel = f"""<section class='panel table-panel'><header class='panel-header'><div><p class='eyebrow'>Control de calidad</p><h2>Filas excluidas</h2></div><span class='panel-meta'>{len(exclusions)} en total</span></header><div class='table-wrap'><table><thead><tr><th>Fila</th><th>Motivo</th></tr></thead><tbody>{exclusion_rows(exclusions)}</tbody></table></div></section>"""
    source_text = ", ".join(esc(source) for source in job["sources"])
    body = f"""<section class='page-heading page-heading--compact'><div><p class='eyebrow'>Detalle y progreso</p><h1>Trabajo <span class='mono'>{esc(job_id)}</span></h1><p class='lede'>Los puntos de control del servidor son la fuente de verdad. Los cambios tardíos quedan bloqueados después de una cancelación o una nueva asignación.</p></div><div class='heading-actions'><a class='button button--secondary' href='/'>Volver al panel</a>{cancel}</div></section>
<section class='job-state'><div><span class='eyebrow'>Estado actual</span><div class='state-line'>{status_badge(state)}<span>Estado vigente del centro de control</span></div>{progress_bar(job["summary"])}<p class='field-hint'>Fuentes: {source_text or "No especificadas"}</p></div><span class='panel-icon' aria-hidden='true'>◫</span></section>
<section class='metric-grid metric-grid--summary'>{summary_cards}</section>{exclusion_panel}"""
    return layout(
        f"Trabajo {job_id}",
        body,
        user=user,
        csrf_token=csrf_token,
        active="jobs",
        team_id=str(job["team_id"]),
    )


def notifications_page(
    service: JobsService,
    user: dict[str, Any],
    *,
    team_id: str,
    csrf_token: str,
) -> str:
    teams = service.list_teams(str(user["id"]))
    selected = team_id or (str(teams[0]["id"]) if len(teams) == 1 else "")
    selector = f"""<section class='panel'><form class='search-bar' method='get' action='/notifications'><label>Equipo<select name='team_id' required>{team_options(teams, selected=selected)}</select></label><span></span><button class='button button--primary' type='submit'>Ver notificaciones</button></form></section>"""
    if not selected:
        body = f"""<section class='page-heading page-heading--compact'><div><p class='eyebrow'>Actividad del equipo</p><h1>Notificaciones</h1><p class='lede'>Elige un equipo para revisar los cambios recientes de sus trabajos.</p></div><a class='button button--secondary' href='/'>Volver al panel</a></section>{selector}"""
        return layout(
            "Notificaciones",
            body,
            user=user,
            csrf_token=csrf_token,
            active="notifications",
        )
    notifications = service.notifications(str(user["id"]), selected)
    rows = (
        "".join(
            f"<tr><td>{esc(EVENT_LABELS.get(item['event_type'], 'Actualización del trabajo'))}</td><td><a class='job-link' href='/jobs/{esc(item['job_id'])}'>{esc(item['job_id'])}</a></td><td>{esc(item['created_at'])}</td><td>{'Entregada' if item['delivery_state'] == 'delivered' else 'Pendiente'}</td></tr>"
            for item in notifications
        )
        or "<tr><td class='empty-cell' colspan='4'>No hay notificaciones para este equipo.</td></tr>"
    )
    body = f"""<section class='page-heading page-heading--compact'><div><p class='eyebrow'>Actividad del equipo</p><h1>Notificaciones</h1><p class='lede'>Cambios recientes de los trabajos del equipo seleccionado.</p></div><a class='button button--secondary' href='/'>Volver al panel</a></section>{selector}<section class='panel table-panel'><header class='panel-header'><div><p class='eyebrow'>Últimos eventos</p><h2>Actividad reciente</h2></div><span class='panel-meta'>Hasta 100 eventos</span></header><div class='table-wrap'><table><thead><tr><th>Evento</th><th>Trabajo</th><th>Fecha</th><th>Entrega</th></tr></thead><tbody>{rows}</tbody></table></div></section>"""
    return layout(
        "Notificaciones",
        body,
        user=user,
        csrf_token=csrf_token,
        active="notifications",
        team_id=selected,
    )


def admin_page(service: JobsService, user: dict[str, Any], csrf_token: str) -> str:
    actor_id = str(user["id"])
    teams = service.list_teams(actor_id)
    users = service.list_users(actor_id)
    csrf = csrf_field(csrf_token)
    options = team_options(teams)
    user_options = "".join(
        f"<option value='{esc(item['id'])}'>{esc(item['email'])}</option>"
        for item in users
    )
    user_rows = (
        "".join(
            f"<tr><td>{esc(item['email'])}</td><td>{'Administrador del sitio' if item['is_site_admin'] else 'Usuario'}</td></tr>"
            for item in users
        )
        or "<tr><td class='empty-cell' colspan='2'>No hay usuarios registrados.</td></tr>"
    )
    team_rows = (
        "".join(
            f"<tr><td>{esc(team['name'])}</td><td>{len(service.team_members(actor_id, str(team['id'])))}</td><td>{len(service.credential_metadata(actor_id, str(team['id'])))}</td></tr>"
            for team in teams
        )
        or "<tr><td class='empty-cell' colspan='3'>No hay equipos registrados.</td></tr>"
    )
    body = f"""<section class='page-heading page-heading--compact'><div><p class='eyebrow'>Control de acceso</p><h1>Administración</h1><p class='lede'>Gestiona equipos, usuarios, membresías y referencias de credenciales sin exponer secretos.</p></div><a class='button button--secondary' href='/'>Volver al panel</a></section>
<section class='work-grid'><section class='panel'><header class='panel-header'><div><p class='eyebrow'>Personas</p><h2>Crear usuario local</h2></div></header><p class='panel-copy'>Entrega una cuenta temporal a un miembro o líder del equipo.</p><form class='stack-form' method='post' action='/ui/admin/user'>{csrf}<label>Correo electrónico<input name='email' type='email' required></label><label>Contraseña temporal<input name='password' type='password' minlength='12' required></label><button class='button button--secondary' type='submit'>Crear usuario</button></form></section>
<section class='panel'><header class='panel-header'><div><p class='eyebrow'>Equipos</p><h2>Crear equipo</h2></div></header><p class='panel-copy'>Los equipos son el límite de acceso para trabajos y resultados publicados.</p><form class='stack-form' method='post' action='/ui/admin/team'>{csrf}<label>Nombre del equipo<input name='name' required></label><button class='button button--secondary' type='submit'>Crear equipo</button></form></section></section>
<section class='panel'><header class='panel-header'><div><p class='eyebrow'>Membresías</p><h2>Asignar acceso</h2></div></header><form class='form-grid' method='post' action='/ui/admin/member'>{csrf}<label>Equipo<select name='team_id'>{options}</select></label><label>Usuario<select name='user_id'>{user_options}</select></label><label>Rol<select name='role'><option value='leader'>Líder de equipo</option><option value='member'>Miembro del equipo</option></select></label><div class='form-actions'><button class='button button--secondary' type='submit'>Guardar membresía</button></div></form></section>
<section class='panel'><header class='panel-header'><div><p class='eyebrow'>Credenciales proxy</p><h2>Configurar proveedor por equipo</h2></div></header><p class='panel-copy'>La configuración se cifra al guardar y nunca se devuelve en este espacio.</p><form class='form-grid' method='post' action='/ui/admin/credential'>{csrf}<label>Equipo<select name='team_id'>{options}</select></label><label>Proveedor<select name='provider'><option value='geonode'>GeoNode</option><option value='dataimpulse'>DataImpulse</option></select></label><label>Referencia del secreto<input name='secret_ref' placeholder='produccion/equipo-a/proxy' required></label><label class='field-span-2'>Configuración de credencial<textarea name='secret_json' placeholder='Configuración JSON' required></textarea></label><div class='form-actions field-span-2'><button class='button button--secondary' type='submit'>Cifrar y guardar credencial</button></div></form></section>
<section class='work-grid'><section class='panel table-panel'><header class='panel-header'><div><p class='eyebrow'>Directorio</p><h2>Usuarios</h2></div></header><div class='table-wrap'><table><thead><tr><th>Correo</th><th>Rol</th></tr></thead><tbody>{user_rows}</tbody></table></div></section><section class='panel table-panel'><header class='panel-header'><div><p class='eyebrow'>Límites de acceso</p><h2>Equipos</h2></div></header><div class='table-wrap'><table><thead><tr><th>Equipo</th><th>Miembros</th><th>Credenciales</th></tr></thead><tbody>{team_rows}</tbody></table></div></section></section>"""
    return layout(
        "Administración", body, user=user, csrf_token=csrf_token, active="admin"
    )


def _is_team_leader(service: JobsService, user: dict[str, Any], team_id: str) -> bool:
    return any(
        member["id"] == str(user["id"]) and member["role"] == "leader"
        for member in service.team_members(str(user["id"]), team_id)
    )
