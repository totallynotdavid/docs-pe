# Seguridad del portal

El portal es una aplicación de navegador con sesiones opacas del lado del servidor.
No hay registro público: la primera cuenta administrativa procede únicamente de
`PORTAL_BOOTSTRAP_ADMIN_EMAIL` y `PORTAL_BOOTSTRAP_ADMIN_PASSWORD`; las demás se
crean desde Administración.

## Controles aplicados

- Las contraseñas nuevas se procesan con `pwdlib` y su configuración Argon2id
  recomendada por FastAPI; la comprobación de una cuenta inexistente usa el hash
  ficticio documentado para no convertir el tiempo de respuesta en un oráculo.
- El identificador de sesión se genera con el CSPRNG de Python, tiene 256 bits de
  aleatoriedad útil, llega al navegador solo como cookie y la base de datos guarda
  únicamente su SHA-256. Las sesiones vencen en el servidor, se renuevan al iniciar
  sesión y se eliminan al salir.
- En producción la cookie es `__Host-portal-id`, `Secure`, `HttpOnly`, `SameSite=Lax`,
  `Path=/` y sin `Domain`; el arranque rechaza un origen que no sea HTTPS. Starlette
  aplica además `HTTPSRedirectMiddleware` y `TrustedHostMiddleware`.
- Cada `POST`, incluido el ingreso y la salida, exige un token CSRF sincronizador
  emitido y validado en el servidor. Las mutaciones también exigen que `Origin` o
  `Referer` coincida exactamente con `PORTAL_PUBLIC_ORIGIN`.
- El inicio de sesión responde de forma genérica, verifica una contraseña ficticia
  si la cuenta no existe y limita a cinco fallos por correo e IP en cinco minutos.
- Las rutas leen la identidad desde la sesión y delegan todas las comprobaciones de
  equipo/rol al servicio: miembros leen y buscan, líderes gestionan procesos y
  credenciales, y administración del sitio gestiona todos los equipos. El SSE vuelve
  a comprobar la sesión y la pertenencia antes de leer cada evento persistido.

## Fuentes consultadas

- [FastAPI: Form y StreamingResponse](https://fastapi.tiangolo.com/tutorial/request-forms/)
  y [respuesta personalizada](https://fastapi.tiangolo.com/advanced/custom-response/).
- [FastAPI: password hashing con `pwdlib`](https://fastapi.tiangolo.com/tutorial/security/oauth2-jwt/).
- [Starlette: `Response.set_cookie`](https://www.starlette.io/responses/) y
  [HTTPS/Trusted Host middleware](https://www.starlette.io/middleware/).
- OWASP: [Authentication](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html),
  [Session Management](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html),
  [CSRF Prevention](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html),
  [Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html),
  [Authorization](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html)
  y [ASVS](https://owasp.org/www-project-application-security-verification-standard/).
