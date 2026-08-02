# Guidelines

## Commands

The toolchain (uv, python, ruff, postgres) is pinned in `mise.toml`. Run tasks from
the repo root:

```sh
mise install          # install the toolchain
mise run install      # uv sync --all-packages --all-groups
mise run format       # ruff format + ruff check --fix
mise run check        # mypy across the workspace
mise run test         # pytest across all packages
mise run build        # PyInstaller single binary for fetch
mise run portal:dev   # start local PostgreSQL, migrate, bootstrap, run the portal
```

Focused work:

```sh
uv run pytest tests/fetch/sites/osiptel/test_lookup.py::test_name
uv run pytest tests/portal        # or tests/browser, etc.
uv run mypy packages/portal
uv run ruff check packages/portal
```

Tests live in one `tests/` package at the repo root, with one subpackage per
workspace member. Keeping a single `tests` package is what lets `mypy .` and `pytest`
each run as one process: four directories all named `tests` collide on that module
name, and every workaround for it (per-package mypy, a non-default pytest import
mode, an `INP001` ignore) disappears once the name is unique.

pytest is configured once in the root `pyproject.toml`: `asyncio_mode = "auto"`, so
async tests are plain `async def test_*` with no decorator.

`mise run test` runs everything, portal included: it depends on `portal:db:start`, and
`tests/portal/conftest.py` defaults to that cluster, so no `PORTAL_TEST_DSN` is needed.
CI sets the variable to override the default; nothing else has to.

**A missing database fails the run, it does not skip it.** The portal is
PostgreSQL-only with no in-memory repository to degrade into, so a green run has to
mean the portal really ran. The `portal_cluster` session fixture dials the DSN once and
calls `pytest.exit` with the command to fix it. Because that is a fixture rather than a
collection hook, `uv run pytest tests/fetch` still works with no cluster at all — only a
test that wants a database demands one.

Each test gets a freshly migrated database of its own and drops it afterwards, so they
are safe to run in parallel and never share state. `portal:db:start` turns off `fsync`,
`synchronous_commit` and `full_page_writes`: `var/postgres` is disposable, and paying
for durability made creating those databases roughly 8x more expensive (23s → 6s across
`tests/portal`). Do not copy that setting anywhere a database is meant to survive.

## Architecture

A uv workspace whose members are `packages/*`, split by **the mechanism a site demands**
rather than by domain layer. The root `readme.md` is the map; each package readme is
authoritative for its own package.

- `packages/fetch` — the workhorse. Bulk lookup over plain HTTP (`osiptel`, `sunat`,
  `sunat_reps`), fanned across async lanes behind sticky proxy sessions, backed by a
  resume database.
- `packages/browser` — sites that need a real Chrome, driven over CDP on a headless
  display (`portabilidad`, `entel`).
- `packages/capture` — the discovery tool to reach for first when adding a site:
  a localhost relay plus a DevTools snippet that intercepts a site's own calls from
  your everyday Chrome. Standard library only.
- `packages/portal` — the authenticated, team-scoped control plane, PostgreSQL-only.
  See also `packages/portal/security.md`.

### The site maturity ladder

The three collector packages are stages, not duplicates. `capture` probes a live site
from your own Chrome and discovers its wire protocol; `browser` drives it for real but
is not yet trusted for unattended runs; `fetch` is the stable, plain-HTTP form. Each
stage rewrites for a different mechanism, so a shared parser would couple a throwaway
probe to a production collector — that is why `capture/ruc.py`, `browser/subject.py`
and `fetch.domain.types.Doc` are separate on purpose.

`Site.stable` is the last rung: the portal offers exactly the sites whose flag is set,
via `fetch.sites.registry.STABLE_SITES`. Promoting a site is that one flag, not an
edit in two packages. `browser`'s sites are absent from the portal because they are
not in `fetch` at all.

### A site is a value, not a class

`fetch/domain/types.py:Site` is a frozen dataclass: name, columns, `accepts(doc)`,
`allows_empty`, tuning, endpoints, and two async functions — `ready(client, site)` warms
a fresh proxy-bound client, `lookup(client, doc)` returns rows. The pipeline, store, and
proxy code are entirely site-agnostic. Adding a site is one new `sites/<name>/` module
plus one entry in `sites/registry.py:SITES`. `browser` and `capture` repeat that same
plain-dict registry shape with their own site types.

### A proxy provider is a value too

`fetch/proxy/base.py:ProviderSpec` mirrors `Site`: a name, a `Field` schema, tuning,
and two functions (`normalize` validates raw strings, `build` returns a live provider).
`fetch/proxy/registry.py:PROVIDERS` is the one registry. That schema is the single
source for all three consumers — the `<PROVIDER>_<FIELD>` environment loader, the
portal's credential form, and the worker that rebuilds a provider from stored values —
so adding a vendor is one module plus one registry line, with no if-chain to extend
and no second copy of a vendor's username format to drift.

`registry.preflight` validates a credential by dialing through a session the provider
itself built, so what it checks is exactly what a run will use.

### The duplication between packages is deliberate

`browser` and `capture` each keep their own copy of a site's parser, columns, and
endpoint, and `browser` keeps a `Doc` vocabulary separate from `fetch`'s. Knowledge
crosses between them through a person, not an import. Do not "fix" this with
cross-package imports. `portal` depends on `fetch` only (workspace source) and
deliberately does not import the unstable `browser`.

### Document routing

`Doc` in `fetch/domain/types.py` normalizes 7–8 digit DNIs (7-digit ones zero-padded to
the canonical 8) and 11-digit RUCs. `RucKind` reads the leading digits to split RUC-10
(natural person → `sunat`) from RUC-20 (entity → `sunat_reps`). The planner routes each
document only to sites whose `accepts` returns true, so no site is handed work it cannot
serve.

### The state DB is the source of truth

Every collector writes `<output>.state.sqlite3` and treats the CSV as a disposable
projection re-exported at the end of each run — atomically, including on `Ctrl-C` and
`SIGTERM`. Re-running the same command resumes. Read progress from the DB, never from
the CSV or a log tail.

### Retry classification has one owner

`fetch/domain/policy.py`: `MAX_ATTEMPTS = 4` attempts per `(doc, site)` pair within a
run; a pair retires permanently only once its cumulative **healthy-contact** attempts
cross `MAX_TOTAL_ATTEMPTS = 12`. Attempts made while the provider's circuit breaker is
open do not count, so an outage cannot grind a valid pair to terminal. Keep fault
classification in that file rather than scattering it into sites.

### Portal layering

`web` (FastAPI, Jinja2/HTMX/SSE) → `application` (team authorization,
submission/cancellation) → `domain` (source planning, state policy) → `repository`
(PostgreSQL transactions); `worker` consumes only the PostgreSQL queue. The web process
never creates schema, users, teams, or credentials — explicit provisioning is the only
path that does.

There is exactly one repository (`repository/postgres.py`), one object store
(`storage/files.py`) and one secret protector (`credentials/secrets.py`). None of them
has a second implementation or a protocol in front of it: the portal is PostgreSQL-only,
so a missing DSN is a startup failure rather than a cue to degrade into something with
different semantics. `portal/security.py` holds password, session and CSRF primitives —
it is below the web layer, not inside it, because `provision.py` and `application/` need
it too.

Inside `web`, `create_app` only builds state and includes the routers in `web/routes/`.
Session, CSRF and adapter lookups are `Depends()` callables in `web/deps.py`, and
`web/errors.py` maps `NotFound` to 404 and every other `PortalError` to 403 once,
rendering the `Problem` page — this is a server-rendered app, so a refusal answers in
HTML, not JSON. A route that lets a `PortalError` escape is denying the request; a route
that catches one is re-rendering its form with `message_for(error)`.

### Code is English; only what a person reads is Spanish

An error names a `Reason`, never a sentence: `raise PermissionDenied(Reason.NOT_A_MEMBER)`.
`portal/messages.py` is the only module that holds Spanish — user-visible copy, proxy
field labels, provider names — so translating the portal is one file and nothing below
the web boundary changes. Template copy and the Spanish URL vocabulary (`/equipos`,
`/ajustes`) stay as they are; identifiers, props, filters and component names do not.

A page and the fragment htmx swaps into it share one URL, chosen by the `HX-Request`
header in `render_hx`. Serving a fragment from its own URL is what made `hx-push-url`
push an address that reloads without a layout. Those routes must keep sending
`Vary: HX-Request`.

Browser assets are vendored, not fetched: `package.json` pins htmx and `htmx-ext-sse`,
`mise run portal:assets` copies them into `web/static`, and CI fails when the committed
copy drifts. That is what lets every response carry `default-src 'self'` with no
exceptions, so no component may use an inline `<script>` or a `style=` attribute — and
`Layout.jinja`'s `htmx-config` meta turns off `includeIndicatorStyles`, because htmx
otherwise injects an inline `<style>` the policy blocks.

### A component owns its markup and its styles

The markup is JinjaX, not Jinja templates: `web/components/<Name>.jinja` is a component
and the `<Name>.css` beside it is loaded only when that component renders, which is what
replaced the single 16 KB `static/portal.css`. `web/pages/<Name>.jinja` are the entry
points routes name — `render("Dashboard")` — and both folders share one namespace, so a
page writes `<UiPanel>` directly. Components carry a `Ui` prefix; pages do not. Props are declared in `{#def #}`; a dynamic value uses
`:prop="expression"`.

`Catalog` collects each rendered component's stylesheet and `render_assets()` in
`Layout.jinja` emits the links. Collection sees one render, so anything htmx can swap in
afterwards — lists, pagination, metrics — has to be declared in `Layout.jinja`'s own
`{#css #}` instead, along with the sheet styling bare `button` elements. A test walks
`pages/*Fragment.jinja` and fails when a component escapes that list.

`web/routes/assets.py` serves the stylesheets from an allowlist read at import. The
folder cannot simply be mounted, because the component templates sit in it too.

Element-level rules (`*`, `html`, `body`, focus rings, `h2`, `p`) and the design tokens
in `static/tokens.css` stay document-scope; they are not any component's property.

## Sharp edges

- `packages/browser/readme.md`'s file map is stale. It describes a direct-CDP backend
  (`backends/direct.py`, `controller.py`, `display.py`) that no longer exists and calls
  Entel the only site. The code is SeleniumBase-based (`backends/seleniumbase.py`, the
  `Session` protocol in `session.py`) and also ships `portabilidad`. Trust the code.
- `GEONODE_COUNTRY=PE` / `DATAIMPULSE_COUNTRY=pe` are mandatory, not defaults: OSIPTEL's
  WAF blocks foreign exits. Provider environment variables follow `<PROVIDER>_<FIELD>`
  from the field schema (`GEONODE_USERNAME`, `GEONODE_LIFETIME_MINUTES`,
  `DATAIMPULSE_SESSION_MINUTES`), so `.env` names change when a field is renamed.
- `pipeline.txt` is the committed runbook for real multi-server batches — validating and
  splitting input, launching detached over ssh, and reading progress from the state DB.
- Ruff runs with a wide `extend-select` and `fix = true`. `portal` has a deliberate
  per-file ignore list in the root `pyproject.toml`; extend it rather than sprinkling
  `noqa`.
- djLint indents the portal components and normalizes their attributes, but it will not
  break a line that mixes markup with inline `{% %}` — pointing it at a file written
  as one long line only wraps attributes at arbitrary columns and leaves that line in
  place. Write the element tree by hand; djLint then holds it. Its config lives in
  `packages/portal/pyproject.toml`, which is the nearest one above them, and needs both
  `extension = "jinja"` and `custom_html = "[A-Z][a-zA-Z0-9]*"` — without the latter it
  does not recognise a component as an element and flattens the tree on contact.
- Components are prefixed `Ui` (`UiPanel`, `UiButton`, `UiMeta`) for one reason: djLint
  lowercases `<Meta>` to the void `<meta>`, silently turning a component into a tag and
  mangling everything after it. The prefix makes a collision impossible, so the old
  "never name a component after an HTML element" rule no longer needs remembering.
  Keep it on every new component.
- djLint splits an inline run like `<strong>x</strong>.` across lines, which collapses to
  a space before the period. `{# djlint:off #}` / `{# djlint:on #}` is the way out, and
  the marker must be exactly that — trailing prose in the same comment stops it matching.
- Run artifacts go under `runs/` (gitignored as a directory), so a fixture CSV
  elsewhere in the tree stays committable. The codebase is `packages/*`.
