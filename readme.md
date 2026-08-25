# docs-pe

Lookup tools for public Peruvian identity, telephone, taxpayer, and portability
data.

## Get started

Install the pinned toolchain and dependencies:

```sh
mise install
mise run install
```

Copy the environment template, add proxy credentials, and run a fetch job:

```sh
cp .env.example .env
uv run --env-file .env fetch \
  --input docs.csv \
  --output results/out.csv \
  --sites osiptel
```

`fetch` writes CSV projections when the run ends and keeps resumable state in
`results/out.state.sqlite3`. The database is the authority for progress. Read
[the operations guide](docs/operations/troubleshooting.md) when a run looks
stuck.

## Packages

| Package                               | Use it for                                                           |
| ------------------------------------- | -------------------------------------------------------------------- |
| [fetch](packages/fetch/readme.md)     | Unattended HTTP lookups through proxy providers.                     |
| [browser](packages/browser/readme.md) | Sites that require Chrome, JavaScript, or a browser gate.            |
| [capture](packages/capture/readme.md) | Discovering a site's request with your own Chrome profile.           |
| [portal](packages/portal/readme.md)   | Web job submission, teams, authentication, and worker orchestration. |

Most new sites move through `capture`, `browser`, and then `fetch`. A site can
stop at any stage. See [Adding a site](docs/adding-a-site.md).

## Read next

- [Architecture](ARCHITECTURE.md), for package boundaries and job state.
- [Proxy configuration](docs/proxies.md), before running a large job.
- [Site references](docs/sites/), for wire behavior and failure modes.
- [Troubleshooting](docs/operations/troubleshooting.md), for runbook commands.
- [Portal deployment](docs/operations/portal-deployment.md), for production
  setup.
- [Worker fleet operations](docs/operations/worker-fleet.md), for adding or
  removing nodes.
- [Historical reports](docs/reports/results.md), for dated measurements only.

## Development

Run the workspace checks from the repository root:

```sh
mise run format
mise run check
mise run test
mise run build
```

The portal's local PostgreSQL loop is available with `mise run dev`. Use
`mise run reset` only when the disposable local database should be removed.
