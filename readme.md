# Getting started

## What is docs-pe?

Takes a CSV of Peruvian DNIs or RUCs and looks each one up on public government
sites. Returns registered phone lines, taxpayer identity records, legal
representatives, and carrier debt.

## Quick start

```sh
mise install                              # install toolchain
mise run install                          # uv sync
cp .env.example .env                      # then fill in proxy credentials
uv run --env-file .env fetch --input docs.csv --output out.csv --sites osiptel
```

The `fetch`, `browser`, and `portal` packages need proxy credentials in `.env`.
`capture` does not. See [proxy configuration](docs/proxies.md) for vendor setup.

## Choose what to read

**I'm running a job for the first time**

- Read [fetch](packages/fetch/readme.md) (the standard tool)

**I'm debugging a job failure**

- Read [troubleshooting](docs/troubleshooting.md)
- Then [docs/sites/](docs/sites/) for the specific site if the runbook doesn't
  cover it

**I'm adding a new site**

- Read [docs/adding-a-site.md](docs/adding-a-site.md) for the full workflow
  (capture → browser → fetch)

**I'm running the portal (web UI)**

- Read [portal](packages/portal/readme.md)

**I'm reviewing code or contributing**

- Start with [docs/architecture.md](docs/architecture.md) to understand the
  system
- Then the package you're reviewing
- Then [CLAUDE.md](CLAUDE.md) for repo conventions

**I want to understand the whole system**

- Read [docs/architecture.md](docs/architecture.md) first
- Then choose a package based on what interests you

## Other commands

```sh
mise run format       # ruff format + ruff check --fix
mise run check        # mypy across workspace
mise run test         # pytest all packages
mise run build        # PyInstaller binary for fetch
mise run dev          # postgres + portal web (Ctrl+C stops everything)
mise run reset        # wipe local postgres; next `mise run dev` starts clean
```

## Structure

This is a
[uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) with
four packages:

| Package                               | Purpose                                                               |
| ------------------------------------- | --------------------------------------------------------------------- |
| [fetch](packages/fetch/readme.md)     | HTTP requests through proxies (the workhorse)                         |
| [browser](packages/browser/readme.md) | Chrome over DevTools protocol (for sites needing JS, reCAPTCHA, etc.) |
| [capture](packages/capture/readme.md) | Reverse-engineer a site using your own Chrome browser                 |
| [portal](packages/portal/readme.md)   | Web UI for managing fetch jobs, auth, teams, results                  |

A new site typically starts in `capture` (discover the request), moves to
`browser` (automate it), then lands in `fetch` (run it unattended).

Each package maintains its own copy of a site's parser and columns. Do not add
cross-package imports; see [docs/architecture.md](docs/architecture.md) for why.

## Job output

Results land in `results/` (which is gitignored). Imports are in `.env` and
config. State is stored in SQLite (`*.state.sqlite3`), which is the source of
truth. Read [docs/architecture.md](docs/architecture.md) for details on the job
lifecycle, resume behavior, and how state works.
