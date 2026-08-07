# Portabilidad

`consulta.portabilidad.pe`. Implemented in
[browser](../../packages/browser/readme.md). Accepts a 9-digit mobile number;
returns carrier-change history: `receptor`, `cedente`, `asignatario_original`,
`fecha_ventana`, `estado`, `current_carrier`.

Gated by Cloudflare Turnstile, not reCAPTCHA. This is the one difference from
Entel that matters operationally: Turnstile tokens expire and session cookies go
stale, but there's no evidence (yet) of the browser-reputation effect documented
for [Entel](entel.md#why-automation-fails). `--control` (the warm-up identifier
`browser` supports) is ignored by this site; it's an Entel-only mechanism.

This page is thin because portabilidad hasn't had the kind of deep investigation
Entel got: no ruled-out-variables list, no measured acceptance rates by profile
age. If you do that investigation, put the findings here rather than in
`packages/browser/readme.md`; this file is the owner of portabilidad-as-a-site
facts.

## See also

- [packages/browser/readme.md](../../packages/browser/readme.md): CLI and
  reject-retry flags
- [entel.md](entel.md): same gate category (bot-detection challenge), much more
  thoroughly documented
