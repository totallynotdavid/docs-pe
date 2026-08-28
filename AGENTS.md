Tooling

The toolchain is pinned in mise.toml and only uv resolves it. Never invoke
python, pytest, ruff, or mypy directly.

```
mise run install       uv sync --all-groups
mise run install:core  reinstall core after editing it; core installs non-editable
mise run format        ruff format + ruff check --fix
mise run check         mypy across the repository
mise run test          pytest across every package, portal included
mise run build         PyInstaller single binary for fetch
mise run dev           start postgres, bootstrap, and run the portal (Ctrl+C stops everything)
mise run reset         wipe the local postgres cluster; next `mise run dev` starts clean
```

Focused work, when a full run is too slow:

```
uv run pytest tests/cli                          no database needed
uv run pytest tests/portal                       starts a postgres cluster
uv run pytest tests/core/sites/osiptel/test_lookup.py::test_name
uv run mypy packages/portal
uv run ruff check packages/portal
```

Read these before changing code

Start with `ARCHITECTURE.md` to understand system structure, job lifecycle, and
shared concepts. Then read the package readme you're working on.

Each package readme covers its own domain:

- `packages/core/readme.md`: HTTP sites, providers, sessions, retry policy
- `packages/cli/readme.md`: CLI, outputs, resume, and sharded jobs
- `packages/browser/readme.md`: Chrome automation, reCAPTCHA/Cloudflare,
  rejection retry
- `packages/capture/readme.md`: reverse-engineer sites using your own Chrome
- `packages/portal/readme.md`: job queue, web UI, provisioning
- `packages/portal/operations.md`: SQL runbook for manual portal intervention

`docs/` covers cross-cutting concerns. Package readmes link into it; don't
restate its content in a package readme:

- `docs/proxies.md`: provider tuning and behavior
- `docs/operations/troubleshooting.md`: runbook, log interpretation
- `docs/sites/`: per-site wire protocol, gates, failure modes (entel, osiptel,
  sunat, portabilidad)
- `docs/adding-a-site.md`: capture → browser → core workflow
- `docs/reports/results.md`: historical job data and reconciliation
- `docs/operations/portal-deployment.md`: Dokploy topology, cloudflared edge,
  master key, tailnet

See `ARCHITECTURE.md` for:

- Package boundaries and why cross-imports are forbidden
- Circuit breaker behavior and its role in preventing cascading failures
- State database as source of truth (why CSVs are disposable, why logs can be
  misleading)
- Retry semantics and attempt counting

A fact about the system belongs in exactly one of these files. If you're about
to write something that's already said elsewhere, link to it instead of
restating it: that's how the previous version of this docs set ended up with the
same GeoNode port-exhaustion note in three files.

Job output goes under `results/`, which is gitignored.

Where things live

add a core site packages/core/core/sites/<name>/ + sites/registry.py SITES
add a core proxy vendor packages/core/core/proxy/<name>.py + proxy/registry.py
PROVIDERS fault or retry behaviour packages/core/core/domain/policy.py, the
only owner document parsing/routing packages/core/core/domain/types.py, Doc
and RucKind CLI resume state packages/cli/cli/store/outcomes.py portal HTTP
handling packages/portal/portal/web/routes/ portal auth and CSRF
packages/portal/portal/web/deps.py, not in a route portal SQL
packages/portal/portal/repository/, one module per concern: auth.py, teams.py,
credentials.py, jobs.py.

Tests live in one tests/ package at the repo root, one subpackage per package.

pytest is configured once in the root pyproject.toml with asyncio_mode = "auto",
so an async test is a plain async def test_* with no decorator.

Rules that break things silently when ignored

Do not add cross-package imports between `capture`, `browser`, and `core`. See
`ARCHITECTURE.md#package-boundaries`.

Do not classify faults inside a site. See `ARCHITECTURE.md`. `domain/policy.py`
owns the mapping from fault to retry action. A site that grows its own retry
rule silently escapes circuit breaker accounting.

Do not read progress from a CSV or log tail. See `ARCHITECTURE.md` and
`docs/operations/troubleshooting.md`. The state database is the source of truth.
Output CSVs don't exist until a run ends; a mid-run empty directory is normal. A
relaunch retries known-bad documents first, so logs open with a burst that looks
like collapse. Query outcomes instead.

Ruff runs with a wide extend-select and fix = true. portal has a deliberate
per-file ignore list in the root pyproject.toml; extend that list rather than
sprinkling noqa.

Writing comments

State the invariant, constraint, or external quirk. Delete anything that
restates the code or labels structure. One idea per comment, placed beside the
code it explains. Prefer the current contract over how the code used to work;
where an approach was tried and rejected, say what was tried and what it cost,
not what changed.

No em dashes. Use a period, comma, colon, or parentheses.

If code needs a comment to be followable, that usually means the approach is
wrong or debt is being kept. Fix the code first.
