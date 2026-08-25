# Portabilidad

`consulta.portabilidad.pe`. Implemented in
[browser](../../packages/browser/readme.md), whose CLI and reject-retry flags
apply here too. Accepts a 9-digit mobile number; returns carrier-change history:
`receptor`, `cedente`, `asignatario_original`, `fecha_ventana`, `estado`,
`current_carrier`.

Cloudflare Turnstile protects the lookup. Tokens expire and session cookies can
go stale. Do not pass `--control`; this site ignores it.
