# portal

Web interface and worker for running `fetch` jobs.

Teams upload documents, choose their proxy credentials, and receive results for
each document.

```sh
mise run portal:dev
```

## Structure

```text
web          FastAPI, JinjaX, HTMX, SSE, sessions, and CSRF
application  Team access, job submission, and cancellation
domain       Source planning and state rules
repository   PostgreSQL, one module per concern: auth.py, teams.py,
             credentials.py, jobs.py; no shared facade
worker       Claims work and publishes results
storage      Immutable object references
```

PostgreSQL stores users, teams, jobs, queue state, and events. A missing
database DSN prevents startup.

`security.py` contains the password, session, and CSRF primitives used by the
web and provisioning code.

## Queue

Every queue transition locks the singleton `portal_queue_control` row. This
enforces the global limit of five active jobs across all web and worker
processes.

Cancellation increments the lease fence before active items are retired. Writes
from an older lease are then rejected.

Items that repeatedly expire without completing are retired. A job that finishes
without publishing any result is marked as failed.

Expired leases are recovered while the next item is claimed.

Workers access the queue through authenticated claim and publish operations. A
claim includes the proxy configuration required for that item.

## Provisioning

The web interface has no public registration. Create the first administrator and
team with:

```sh
uv run --env-file .env python -m portal.provision \
  --admin-email admin@example.org \
  --admin-password-env PORTAL_PROVISION_ADMIN_PASSWORD \
  --team-name "Equipo Lima" \
  --team-slug equipo-lima
```

Add `--proxy-provider geonode` or `--proxy-provider dataimpulse` to create proxy
credentials from `PORTAL_PROVISION_<PROVIDER>_<FIELD>` environment variables.

The command applies migrations and creates or updates the administrator, team,
membership, and proxy credentials. It never prints secret values.

## Local development

Set these values in the root `.env`:

```bash
PORTAL_DATABASE_DSN=postgresql://postgres@127.0.0.1:5432/postgres
PORTAL_PUBLIC_ORIGIN=http://localhost:8000
PORTAL_BOOTSTRAP_ADMIN_EMAIL=admin@example.org
PORTAL_BOOTSTRAP_ADMIN_PASSWORD=choose-a-local-password
PORTAL_BOOTSTRAP_TEAM_NAME="Equipo Lima"
PORTAL_BOOTSTRAP_TEAM_SLUG=equipo-lima
PORTAL_SECRET_PROTECTION_KEY=
```

Then run:

```sh
mise run portal:dev
```

The command starts PostgreSQL in `.data/postgres` when port 5432 is unused,
applies migrations, provisions the local administrator and team, and starts the
server.

`PORTAL_SECRET_PROTECTION_KEY` is required to encrypt proxy credentials.
Generate one with:

```bash
uv run python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

Apply migrations without starting the server:

```sh
mise run portal:migrate
```

Run only the local provisioning step:

```sh
mise run portal:bootstrap
```

## Presentation

Pages use JinjaX, HTMX, and SSE.

Components live in `web/components/<Name>.jinja`. A component stylesheet uses
the same name and loads only when that component renders.

Route pages live in `web/pages/<Name>.jinja`. Components use a `Ui` prefix;
pages do not.

Shared design tokens live in `web/static/tokens.css`. Component-specific styles
stay beside their component.

HTMX and its SSE extension are vendored in `web/static`:

```sh
mise run portal:assets
```

CI checks that the committed files match the versions in
`packages/portal/package.json`.

Responses use this content security policy:

```text
Content-Security-Policy: default-src 'self'
```

Inline scripts and `style=` attributes are not allowed.

Full pages and HTMX fragments share the same URL. Routes return
`Vary: HX-Request` so caches keep both response types separate.

## Language and errors

Spanish user-facing text lives in `messages.py`.

`web/errors.py` maps `NotFound` to 404 and other `PortalError` values to 403.

Routes may catch a `PortalError` when they need to render the same form with an
error message. Otherwise the shared error handler renders the problem page.

## Tests

```sh
uv run pytest tests/portal
```

Each test creates and migrates its own PostgreSQL database, then drops it after
the test.

Tests may run in parallel because they do not share database state. If
PostgreSQL is unavailable, the suite fails.
