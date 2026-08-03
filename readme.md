# docs-pe

Takes a CSV of Peruvian DNIs or RUCs and looks each one up on the public sites
that answer for it. Returns registered phone lines, taxpayer identity records,
legal representatives, and carrier debt.

## Get started

```sh
mise install                                  # toolchain: uv, python, ruff, postgres
mise run install                              # uv sync --all-packages --all-groups
cp .env.example .env                          # then fill in the proxy credentials
uv run fetch --input docs.csv --output out.csv --sites osiptel
```

`fetch` needs proxy credentials; `browser` and `capture` do not. The
[fetch manual](packages/fetch/readme.md) is the place to start reading.

Other tasks, all from the repo root:

```sh
mise run format       # ruff format + ruff check --fix
mise run check        # mypy across the workspace
mise run test         # pytest across all packages, portal included
mise run build        # PyInstaller single binary for fetch
mise run portal:dev   # start PostgreSQL, migrate, bootstrap, run the portal
```

## Packages

A [uv workspace](https://docs.astral.sh/uv/concepts/projects/workspaces/). Each
package serves one access mechanism.

| Package                                 | Reads sites that             |                                                   |
| --------------------------------------- | ---------------------------- | ------------------------------------------------- |
| [`fetch`](packages/fetch/readme.md)     | answer a plain HTTP request  | the workhorse, and the one you almost always want |
| [`browser`](packages/browser/readme.md) | need a real Chrome           | driven over CDP on a headless display             |
| [`capture`](packages/capture/readme.md) | need a browser a person owns | the discovery tool, standard library only         |
| [`portal`](packages/portal/readme.md)   | are stable enough to sell    | authenticated, team-scoped, PostgreSQL-only       |

A new site usually starts in `capture`, which discovers its wire protocol from
your own Chrome, moves to `browser` once it can be driven, and lands in `fetch`
when it survives unattended runs. `Site.stable` is the last step: the portal
offers exactly the sites whose flag is set.

Each package keeps its own copy of a site's parser, columns, and document type.
Do not add cross-package imports.

## Notes

Written while working out how the sites behave.

- [docs/results.md](docs/results.md), every reconciled job and what a healthy
  one looks like.
- [docs/proxies.md](docs/proxies.md), how the proxy vendors behave per site.
- [docs/entel.md](docs/entel.md), the Entel wire protocol and everything ruled
  out.

Job output lands in `results/`, which is gitignored.
