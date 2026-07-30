# Portal OSIPTEL

`packages/portal` is the new team-scoped portal foundation. It is separate from
`packages/jobs`, which remains deployed and unchanged during this stage. The CRM is
a visual reference only; it is not a dependency or ownership model.

## Desarrollo local

Install the workspace dependencies with `mise run install`. Copy `.env.example` and
set `PORTAL_DATABASE_DSN` to a PostgreSQL database. Apply migrations in lexical order
with the `portal.migrations.apply_migrations` deployment bootstrap; it records each
filename in `portal_schema_migrations`. A deployment must run migrations before web
or worker processes. Foundation tests do not need a database; the migration remains
executable PostgreSQL for integration environments.

Run the focused suite with `uv run pytest packages/portal/tests`, format with
`uv run ruff format packages/portal`, lint with `uv run ruff check packages/portal`,
and type-check with `uv run mypy packages/portal`.

## Límites de proceso

`portal.web` is a FastAPI operational boundary. Browser pages will use Jinja2, HTMX,
and SSE, but this foundation deliberately exposes only `/salud` and `/listo`.
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
