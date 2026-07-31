# Portal OSIPTEL

La configuración y las garantías de la experiencia autenticada están en
[security.md](security.md).

`packages/portal` is the new team-scoped portal foundation. It is separate from
`packages/jobs`, which remains deployed and unchanged during this stage. The CRM is
a visual reference only; it is not a dependency or ownership model.

## Desarrollo local

Install the workspace dependencies with `mise run install`. Copy `.env.example` and
set `PORTAL_DATABASE_DSN` to a PostgreSQL database. `mise run portal:dev` starts the
repo-local PostgreSQL cluster in `var/postgres` when nothing is already listening on
`127.0.0.1:5432`, applies pending schema migrations, idempotently provisions the
local administrator and first team, then starts the portal. Set these local bootstrap
variables in the root `.env` before using that command:

```bash
PORTAL_BOOTSTRAP_ADMIN_EMAIL=admin@example.org
PORTAL_BOOTSTRAP_ADMIN_PASSWORD=choose-a-local-password
PORTAL_BOOTSTRAP_TEAM_NAME="Equipo Lima"
PORTAL_BOOTSTRAP_TEAM_SLUG=equipo-lima
```

The web process itself never creates schema, users, teams, or credentials. Generate a
local secret-protection key once, without committing it:

```bash
uv run python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

Provisioning is explicit and idempotent; web-process startup never creates users,
teams, credentials, or schema state. Use `uv` to inject the local environment file:

```bash
uv run --env-file packages/portal/.env python -m portal.provision \
  --admin-email admin@example.org \
  --admin-password-env PORTAL_PROVISION_ADMIN_PASSWORD \
  --team-name "Equipo Lima" \
  --team-slug equipo-lima
```

For a production-like explicit setup, use the provisioning command above. To apply
only pending schema migrations, run `mise run portal:migrate`; to run the idempotent
local setup without starting the server, run `mise run portal:bootstrap`. Start the
local portal with:

```bash
mise run portal:dev
```

Add `--proxy-provider geonode` or `--proxy-provider dataimpulse` to configure a
provider from the named `PORTAL_PROVISION_*` variables. The command applies and
verifies migrations, creates or finds the administrator/team/leader relationship,
and prints only names and status. It never prints record identifiers or credentials.
`PORTAL_SECRET_PROTECTION_KEY` enables the AES-GCM adapter in every environment;
the existing `PORTAL_SECRET_KEY` deployment secret is accepted as its compatible
fallback. In production, inject either as a deployment secret (never commit it); a
dedicated secret-manager/KMS adapter can replace it later. No cloud provider is
selected here.

Run the focused suite with `uv run pytest packages/portal/tests`, format with
`uv run ruff format packages/portal`, lint with `uv run ruff check packages/portal`,
and type-check with `uv run mypy packages/portal`.

## Límites de proceso

`portal.web` is a FastAPI operational boundary. Browser pages use Jinja2, HTMX, and
SSE. It opens dependencies and serves readiness only; deployment provisioning is the
sole path that applies schema changes or creates initial data.
`portal.application` holds team authorization and submission/cancellation use cases;
`portal.domain` contains source planning and state policy; `portal.repository` owns
PostgreSQL transactions; `portal.worker` consumes only that PostgreSQL queue.
`portal.web.static/tokens.css` establishes a light-only token layer so a later dark
theme can replace tokens without changing page components.

`portal.storage.port` accepts immutable, provider-neutral object references, never a
local process path. `portal.integrations.port` describes notification delivery, but
does not send email or Kapso WhatsApp messages. PostgreSQL persists both `job_events`
and `notification_outbox` intent for in-app, email, and Kapso WhatsApp delivery.

The singleton `portal_queue_control` row is locked by every admission, cancellation,
completion, and FIFO promotion transaction. It fixes the global running/cancelling
limit at five. Cancellation increments a fence before retiring active items, so late
worker writes are rejected; already published object references remain available.

## Trabajo posterior

Add Spanish Jinja/HTMX/SSE pages, a concrete immutable object-store adapter,
notification senders and delivery retries, authentication/session handlers, and
deployment adapters (pool lifecycle, worker registration, and PostgreSQL health ping).
Keep the light-token theme extensible for a future dark theme; do not introduce a
second queue, global credential picker, browser source, or local-file workflow.
