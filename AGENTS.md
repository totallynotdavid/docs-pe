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

The real PostgreSQL contract tests in `tests/portal/test_postgres_queue.py`
**skip silently** unless `PORTAL_TEST_DSN` is set. A green `mise run test` does not mean
the queue contract ran.

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

### A site is a value, not a class

`fetch/domain/types.py:Site` is a frozen dataclass: name, columns, `accepts(doc)`,
`allows_empty`, tuning, endpoints, and two async functions — `ready(client, site)` warms
a fresh proxy-bound client, `lookup(client, doc)` returns rows. The pipeline, store, and
proxy code are entirely site-agnostic. Adding a site is one new `sites/<name>/` module
plus one entry in `sites/registry.py:SITES`. `browser` and `capture` repeat that same
plain-dict registry shape with their own site types.

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

Inside `web`, `create_app` only builds state and includes the routers in `web/routes/`.
Session, CSRF and adapter lookups are `Depends()` callables in `web/deps.py`, and
`web/errors.py` maps `NotFound` to 404 and every other `PortalError` to 403 once. A route
that lets a `PortalError` escape is denying the request; a route that catches one is
re-rendering its form with a message.

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
page writes `<Panel>` directly. Props are declared in `{#def #}`; a dynamic value uses
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
  WAF blocks foreign exits.
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
- **Never name a component after an HTML element.** djLint lowercases `<Meta>` to the
  void `<meta>`, which silently turns the component into a tag and mangles everything
  after it; that is why the class `.meta` is rendered by `MetaDato`. `Search` and
  `Envio` are safe only because no template writes them as tags.
- djLint splits an inline run like `<strong>x</strong>.` across lines, which collapses to
  a space before the period. `{# djlint:off #}` / `{# djlint:on #}` is the way out, and
  the marker must be exactly that — trailing prose in the same comment stops it matching.
- The repo root holds many untracked run artifacts (`*.csv`, `*.log`,
  `*_out.state.sqlite3`) plus leftover `robot/`, `tests/`, and `.old-state-backup/`
  directories that contain no tracked source. The codebase is `packages/*`.
