TOOLING

The toolchain is pinned in mise.toml and only uv resolves it. Never invoke
python, pytest, ruff, or mypy directly.

```
mise run install     uv sync --all-packages --all-groups
mise run format      ruff format + ruff check --fix
mise run check       mypy across the workspace
mise run test        pytest across every package, portal included
mise run build       PyInstaller single binary for fetch
mise run portal:dev  start postgres, migrate, bootstrap, run the portal
```

Focused work, when a full run is too slow:

```
uv run pytest tests/fetch                        no database needed
uv run pytest tests/portal                       starts a postgres cluster
uv run pytest tests/fetch/sites/osiptel/test_lookup.py::test_name
uv run mypy packages/portal
uv run ruff check packages/portal
```

READ THIS BEFORE TOUCHING THAT

Each readme is authoritative for its own package and is written to be read in
full before you change anything in it. Reading the readme is cheaper than
reading the code.

- packages/fetch/readme.md: sites, proxy config, outputs, resume and retry
  semantics
- packages/browser/readme.md: CDP automation, the local proxy, reject retry
  policy
- packages/capture/readme.md: discovery relay
- packages/portal/readme.md: layers, the queue, provisioning, presentation
- packages/portal/security.md
- readme.md: the workspace map

Knowledge that outlives a job belongs in docs/. Check it before re-deriving
anything about proxies, throughput, or Entel. Retry old ideas only if there's
reason to believe conditions have changed.

- docs/results.md: every reconciled job, expected shapes, what a job costs
- docs/proxies.md: vendor behaviour per site, lane counts, how to read a run
- docs/entel.md: Entel wire protocol and everything ruled out

Job output goes under results/, which is gitignored.

WHERE THINGS LIVE

add a fetch site packages/fetch/fetch/sites/<name>/ + sites/registry.py SITES
add a proxy vendor packages/fetch/fetch/proxy/<name>.py + proxy/registry.py
PROVIDERS fault or retry behaviour packages/fetch/fetch/domain/policy.py, the
only owner document parsing/routing packages/fetch/fetch/domain/types.py, Doc
and RucKind resume state packages/fetch/fetch/store/outcomes.py portal HTTP
handling packages/portal/portal/web/routes/ portal auth and CSRF
packages/portal/portal/web/deps.py, not in a route portal SQL
packages/portal/portal/repository/postgres.py, the only one any Spanish string
packages/portal/portal/messages.py, the only place

Tests live in one tests/ package at the repo root, one subpackage per workspace
member.

pytest is configured once in the root pyproject.toml with asyncio_mode = "auto",
so an async test is a plain async def test_* with no decorator.

RULES THAT BREAK THINGS SILENTLY WHEN IGNORED

Do not add cross-package imports. browser, capture, and fetch each keep their
own copy of a site's parser, columns, and document vocabulary. portal imports
fetch only.

Do not classify faults inside a site. domain/policy.py owns the mapping from
fault to retry action, and a site that grows its own retry rule silently escapes
the breaker accounting.

Do not read progress from a CSV or a log tail. The state database is the source
of truth. Output CSVs do not exist until a run ends, so a mid-run directory with
no output is normal, not a broken run. A relaunch retries known-bad documents
first, so the log opens with a wall of failures from a tiny fraction of the
input and reads like a collapse. Query outcomes instead.

GEONODE_COUNTRY=PE and DATAIMPULSE_COUNTRY=pe must be set explicitly in every
.env. OSIPTEL's WAF blocks foreign exits. Provider variables are derived as
<PROVIDER>_<FIELD> from the field schema, so renaming a field renames the
variable.

Ruff runs with a wide extend-select and fix = true. portal has a deliberate
per-file ignore list in the root pyproject.toml; extend that list rather than
sprinkling noqa.

WRITING COMMENTS

State the invariant, constraint, or external quirk. Delete anything that
restates the code or labels structure. One idea per comment, placed beside the
code it explains. Prefer the current contract over how the code used to work;
where an approach was tried and rejected, say what was tried and what it cost,
not what changed.

No em dashes. Use a period, comma, colon, or parentheses.

If code needs a comment to be followable, that usually means the approach is
wrong or debt is being kept. Fix the code first.
