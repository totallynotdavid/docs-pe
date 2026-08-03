# portal

The authenticated, team-scoped control plane: a server-rendered web app plus a
worker that drains a PostgreSQL queue. Teams submit jobs of documents, the
worker runs them through the stable `fetch` adapters using the team's own proxy
credentials, and results are published back per item.

Separately deployable. It depends on `fetch` as a workspace source and
deliberately does not import the unstable `browser`.

```sh
mise run portal:dev
```

The security posture a customer cares about is in [security.md](security.md), in
Spanish.

## Layers

```
web          FastAPI, JinjaX, HTMX, SSE. Sessions, CSRF, browser security.
application  Team authorization, submission, cancellation.
domain       Source planning and state policy. No database or HTTP imports.
repository   PostgreSQL transactions, leases, fencing, queue control.
worker       An outbound client. Never a second queue or a rule owner.
storage      Immutable, provider-neutral object references.
```

PostgreSQL is the only control plane. There is exactly one repository
(`repository/postgres.py`), one object store (`storage/files.py`), and one
secret protector (`credentials/secrets.py`), and none of them has a second
implementation or a protocol in front of it. A missing DSN is a startup failure.

`security.py` sits below the web layer, because `provision.py` and
`application/` need password, session, and CSRF primitives too.

## The queue

A singleton `portal_queue_control` row is locked by every admission,
cancellation, completion, and FIFO promotion. That makes the global limit of
five running-or-cancelling processes exact across every web and worker process,
without a second queue to keep in step.

Cancellation increments a fence before retiring active items, so a late worker
write is rejected while already published object references stay available. An
item that is repeatedly leased without completing retires, and a job that drains
without publishing anything fails rather than reporting an empty success.

Recovery for a worker that died holding a lease runs inside the claim
transaction. Every queue transition already serializes on the queue-control
gate, so a reaper process would contend for the same row and stall the queue
when it died.

The worker's only view of the queue is an authenticated claim/publish pair. It
receives the decrypted proxy grant for its leased item and nothing else.

## Provisioning is the only path in

The web process never creates schema, users, teams, or credentials. There is no
public registration: the first administrator and their first team come from
`python -m portal.provision`, and everyone else is managed from the admin pages.

```sh
uv run --env-file .env python -m portal.provision \
  --admin-email admin@example.org \
  --admin-password-env PORTAL_PROVISION_ADMIN_PASSWORD \
  --team-name "Equipo Lima" \
  --team-slug equipo-lima
```

Add `--proxy-provider geonode` or `--proxy-provider dataimpulse` to configure a
provider from the matching `PORTAL_PROVISION_<PROVIDER>_<FIELD>` variables,
which the engine's field schema drives. The command applies and verifies
migrations, creates or finds the administrator, team, and leader relationship,
and prints only names and status. It may name a missing environment variable but
never prints its value, a record identifier, or a credential.

## Local development

Set these in the root `.env`, then run `mise run portal:dev`. It starts the
repo-local PostgreSQL cluster in `.data/postgres` when nothing is listening on
`127.0.0.1:5432`, applies migrations, provisions the local administrator and
first team idempotently, and starts the server.

```bash
PORTAL_DATABASE_DSN=postgresql://postgres@127.0.0.1:5432/postgres
PORTAL_PUBLIC_ORIGIN=http://localhost:8000
PORTAL_BOOTSTRAP_ADMIN_EMAIL=admin@example.org
PORTAL_BOOTSTRAP_ADMIN_PASSWORD=choose-a-local-password
PORTAL_BOOTSTRAP_TEAM_NAME="Equipo Lima"
PORTAL_BOOTSTRAP_TEAM_SLUG=equipo-lima
PORTAL_SECRET_PROTECTION_KEY=
```

`PORTAL_SECRET_PROTECTION_KEY` enables the AES-GCM adapter and is required in
every environment. Generate one without committing it:

```bash
uv run python -c "import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
```

`mise run portal:migrate` applies migrations alone; `mise run portal:bootstrap`
runs the idempotent local setup without starting the server.

## Presentation

Pages are JinjaX components driven by HTMX and SSE.
`web/components/<Name>.jinja` is a component and the `<Name>.css` beside it
loads only when that component renders; `web/pages/<Name>.jinja` are the entry
points routes name. Both folders share one namespace, so a page writes
`<UiPanel>` directly. Components carry a `Ui` prefix; pages do not.

There is no global stylesheet to grow. Element-level rules and the design tokens
in `static/tokens.css` stay document-scope, because they are no component's
property. Tokens cover both colour schemes, so a component styles neither.

Browser assets are vendored: `packages/portal/package.json` pins htmx and its
SSE extension, `mise run portal:assets` copies them into `web/static`, and CI
fails when the committed copy drifts. That is what lets every response carry
`Content-Security-Policy: default-src 'self'` with no exceptions. No component
may use an inline `<script>` or a `style=` attribute.

A page and the fragment HTMX swaps into it share one URL, chosen by the
`HX-Request` header. Those routes send `Vary: HX-Request`, so a cache cannot
replay a bare fragment into a navigation.

## Language

An error names a `Reason`, never a sentence. `messages.py` is the only module
that holds Spanish: user-visible copy, proxy field labels, provider names.
Translating the portal is one file, and nothing below the web boundary changes.
The Spanish URL vocabulary (`/equipos`, `/ajustes`) and template copy stay as
they are; identifiers, props, filters, and component names do not.

`web/errors.py` maps `NotFound` to 404 and every other `PortalError` to 403
exactly once, rendering the `Problem` page in HTML. A route that lets a
`PortalError` escape is denying the request; a route that catches one is
re-rendering its form.

## Tests

`uv run pytest tests/portal` runs the focused suite against real PostgreSQL.
Each test gets a freshly migrated database of its own and drops it afterwards,
so tests are safe in parallel and never share state.

A missing database fails the run rather than skipping it, so a green run always
means the portal really ran against PostgreSQL.
