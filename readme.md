# docs-pe

Command-line and browser tools for looking up public Peruvian identity,
telephone, taxpayer, and number-portability data.

## Get started

Install the pinned toolchain and dependencies:

```sh
mise install
mise run install
```

Create a CSV with one authorized identifier in its first column. Replace the
example before running a lookup:

```sh
printf '%s\n' '12345678' > subjects.csv
cp .env.example .env
```

Add credentials for the proxy provider in `.env`, then run a small lookup:

```sh
uv run --env-file .env fetch \
  --input subjects.csv \
  --output results/out.csv \
  --sites osiptel
```

The run stores its durable state in `results/out.state.sqlite3` and exports
site-specific CSV projections such as `results/out.osiptel.csv`. A valid lookup
with no returned rows has an `ok` state but no result row. Use the state
database when reconciling a run.

Inspect a completed or interrupted run with:

```sh
uv run fetch-status --output results/out.csv
```

## Choose a runner

| Tool                                    | Use it when                                                                 |
| --------------------------------------- | --------------------------------------------------------------------------- |
| [`fetch`](packages/cli/readme.md)       | The site accepts ordinary HTTP requests and the job needs unattended scale. |
| [`browser`](packages/browser/readme.md) | The site needs Chrome, JavaScript, or a browser reputation signal.          |
| [`capture`](packages/capture/readme.md) | You need to discover a request with your own Chrome profile.                |
| [`portal`](packages/portal/readme.md)   | People need job submission, teams, reusable results, and a worker fleet.    |

New site work usually starts with [`capture`](packages/capture/readme.md), then
moves to [`browser`](packages/browser/readme.md) or
[`fetch`](packages/cli/readme.md) once the protocol is understood. See
[Adding a site](docs/adding-a-site.md).

## Learn more

- [Architecture](ARCHITECTURE.md) defines package ownership and durable state.
- [Site notes](docs/sites/) describe current request and response contracts.
- [Operations](docs/operations/) contains deployment and diagnosis runbooks.
- [Historical measurements](docs/reports/results.md) are dated observations, not
  runtime guarantees.

## Contributing

Read [Contributing](CONTRIBUTING.md) before changing code. It covers the pinned
toolchain, focused checks, documentation ownership, and commit messages.

```sh
mise run install
mise run check
mise run test
```
