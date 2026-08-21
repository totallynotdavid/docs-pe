# docs-pe

[![ci](https://github.com/totallynotdavid/phone-numbers-by-carrier/actions/workflows/ci.yml/badge.svg)](https://github.com/totallynotdavid/phone-numbers-by-carrier/actions/workflows/ci.yml)

Takes a CSV of Peruvian DNIs or RUCs and looks each one up on public government
sites. Returns registered phone lines, taxpayer identity records, legal
representatives, and carrier debt.

## Getting started

```sh
mise install                              # install toolchain
mise run install                          # uv sync
cp .env.example .env                      # then fill in proxy credentials
uv run --env-file .env fetch --input docs.csv --output out.csv --sites osiptel
```

The `fetch`, `browser`, and `portal` packages need proxy credentials in `.env`;
`capture` does not. See [docs/proxies.md](docs/proxies.md) for vendor setup.

Other tasks, all from the repo root:

```sh
mise run format                           # ruff format + ruff check --fix
mise run check                            # mypy across workspace
mise run test                             # pytest all packages
mise run build                            # PyInstaller binary for fetch
mise run dev                              # postgres + portal web (Ctrl+C stops everything)
mise run reset                            # wipe local postgres; next `mise run dev` starts clean
```

This is a
[uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/) with
four packages. [fetch](packages/fetch/readme.md) makes HTTP requests through
proxies and is the workhorse; [browser](packages/browser/readme.md) drives
Chrome over the DevTools protocol for sites that need JS, reCAPTCHA, or similar;
[capture](packages/capture/readme.md) reverse-engineers a site using your own
Chrome browser; [portal](packages/portal/readme.md) is the web UI for managing
fetch jobs, auth, teams, and results. A new site typically starts in `capture`
(discover the request), moves to `browser` (automate it), then lands in `fetch`
(run it unattended). Each package keeps its own copy of a site's parser and
columns rather than sharing code; see
[docs/architecture.md](docs/architecture.md) for why.

Results land in `results/` (gitignored). State is stored in SQLite
(`*.state.sqlite3`), which is the source of truth; see
[docs/architecture.md](docs/architecture.md) for the job lifecycle, resume
behavior, and how state works.

## Read next

- [fetch](packages/fetch/readme.md), if you're running a job for the first time
- [docs/troubleshooting.md](docs/troubleshooting.md), if you're debugging a job
  failure, then [docs/sites/](docs/sites/) for the specific site
- [docs/adding-a-site.md](docs/adding-a-site.md), if you're adding a new site
- [portal](packages/portal/readme.md), if you're running the web UI, then
  [docs/portal-deployment.md](docs/portal-deployment.md) if you're deploying it
- [docs/architecture.md](docs/architecture.md), if you're reviewing code or want
  to understand the whole system before picking a package
