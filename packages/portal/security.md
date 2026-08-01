# Seguridad del portal

El portal es una aplicación de navegador con sesiones opacas del lado del servidor.
No hay registro público: la primera cuenta administrativa y su primer equipo se
crean únicamente con `python -m portal.provision`; las demás personas se gestionan
desde Administración. El arranque web no crea cuentas, equipos ni credenciales.

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
  equipo/rol al servicio: miembros solo leen y buscan; líderes gestionan miembros,
  procesos y credenciales de su propio equipo; administración del sitio gestiona
  personas y estructura sin obtener acceso implícito a datos de un equipo. El SSE
  vuelve a comprobar la sesión y la pertenencia antes de leer cada evento persistido.
- Toda respuesta lleva `Content-Security-Policy: default-src 'self'` sin excepciones:
  htmx y su extensión SSE se sirven desde `web/static` en la versión que fija
  `package.json`, y ningún componente usa script ni estilo en línea. Un CDN
  comprometido deja de ser un camino hacia las credenciales de proxy del equipo.
- Las hojas de estilo de los componentes se sirven desde una lista de permitidos
  (`web/routes/assets.py`), no montando la carpeta: allí también viven las plantillas
  `.jinja`, y publicarlas expondría el marcado del portal.
- Los datos de GeoNode y DataImpulse se normalizan del lado del servidor, se protegen
  antes de persistirse y no se muestran ni se registran. La interfaz nunca recibe
  material cifrado ni claves. El adaptador local usa AES-GCM con una clave inyectada
  por entorno; producción debe inyectar un adaptador de gestor de secretos/KMS antes
  de configurar credenciales. Los errores de preflight son deliberadamente genéricos.

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
  [Authorization](https://cheatsheetseries.owasp.org/cheatsheets/Authorization_Cheat_Sheet.html),
  [Cryptographic Storage](https://cheatsheetseries.owasp.org/cheatsheets/Cryptographic_Storage_Cheat_Sheet.html)
  y [Error Handling](https://cheatsheetseries.owasp.org/cheatsheets/Error_Handling_Cheat_Sheet.html)
  y [ASVS](https://owasp.org/www-project-application-security-verification-standard/).
