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
- The repo root holds many untracked run artifacts (`*.csv`, `*.log`,
  `*_out.state.sqlite3`) plus leftover `robot/`, `tests/`, and `.old-state-backup/`
  directories that contain no tracked source. The codebase is `packages/*`.
