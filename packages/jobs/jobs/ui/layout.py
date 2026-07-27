from __future__ import annotations

from typing import Any

from jobs.ui.components import csrf_field, esc
from jobs.ui.styles import styles


def layout(
    title: str,
    body: str,
    *,
    user: dict[str, Any] | None = None,
    csrf_token: str = "",
    active: str = "dashboard",
    team_id: str = "",
) -> str:
    page_title = esc(title)
    if user is None:
        return f"""<!doctype html><html lang='es'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'><title>{page_title} · Trabajos OSIPTEL</title>
<style>{styles()}</style></head><body class='auth-page'>
<main class='auth-shell'><a class='brand brand--auth' href='/'>
<span class='brand-mark' aria-hidden='true'><svg viewBox='0 0 24 24'><path d='M5 5.5h14v13H5zM8 9h8M8 12h5M8 15h8'/></svg></span>
<span><strong>OSIPTEL</strong><small>Operaciones de trabajos</small></span></a>
<section class='auth-card'><p class='eyebrow'>Espacio protegido</p><h1>{page_title}</h1>{body}</section>
<p class='auth-footer'>Datos por equipo · control de trabajos · credenciales protegidas</p></main>
</body></html>"""

    email = esc(user["email"])
    csrf = csrf_field(csrf_token)
    initials = esc(
        "".join(part[:1] for part in email.split("@", 1)[0].split("."))[:2].upper()
        or "U"
    )
    links = [
        ("dashboard", "/", "▦", "Panel de trabajos"),
        ("jobs", "/#jobs", "◫", "Trabajos"),
        ("search", "/search", "⌕", "Resultados"),
        (
            "notifications",
            f"/notifications{('?team_id=' + esc(team_id)) if team_id else ''}",
            "•",
            "Notificaciones",
        ),
    ]
    if user["is_site_admin"]:
        links.append(("admin", "/admin", "⌘", "Administración"))
    navigation = "".join(
        f"<a class='nav-item {'nav-item--active' if key == active else ''}' href='{href}'><span aria-hidden='true'>{icon}</span>{label}</a>"
        for key, href, icon, label in links
    )
    return f"""<!doctype html><html lang='es'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'><title>{page_title} · Trabajos OSIPTEL</title>
<style>{styles()}</style></head><body class='app-page'>
<div class='app-shell'><aside class='navigation-drawer'>
  <a class='brand' href='/'><span class='brand-mark' aria-hidden='true'><svg viewBox='0 0 24 24'><path d='M5 5.5h14v13H5zM8 9h8M8 12h5M8 15h8'/></svg></span><span><strong>OSIPTEL</strong><small>Operaciones de trabajos</small></span></a>
  <nav aria-label='Navegación principal' class='nav-list'>{navigation}</nav>
  <div class='drawer-footer'><span class='environment-dot'></span><span>Centro de control activo</span></div>
</aside>
<main class='app-main'><header class='topbar'><div class='breadcrumb'><span>Operaciones</span><span aria-hidden='true'>/</span><strong>{page_title}</strong></div>
  <div class='account'><span class='avatar' aria-label='Usuario actual'>{initials}</span><span class='account-email'>{email}</span><form method='post' action='/logout'>{csrf}<button class='icon-button' aria-label='Cerrar sesión' title='Cerrar sesión' type='submit'>↗</button></form></div></header>
  <div class='page-content'>{body}</div>
</main></div></body></html>"""
