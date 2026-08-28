# docs-pe

Tools for looking up public Peruvian identity, telephone, taxpayer, and
number-portability data.

## Start

Install the pinned toolchain and workspace dependencies:

```sh
mise install
mise run install
```

Copy `.env.example`, add the credentials for the proxy provider you intend to
use, and run a small job first:

```sh
cp .env.example .env
uv run --env-file .env fetch \
  --input docs.csv \
  --output results/out.csv \
  --sites osiptel
```

`fetch` stores progress in `results/out.state.sqlite3` and writes CSV files
from that state. The SQLite database is the record of what happened. The CSV
files are disposable projections and may be absent until the process exports
its current state.

Inspect a completed or interrupted run with:

```sh
uv run fetch-status --output results/out.csv
```

## Choose a tool

| Tool | Use it when |
| --- | --- |
| [`fetch`](packages/fetch/readme.md) | The site accepts ordinary HTTP requests and the job needs unattended scale. |
| [`browser`](packages/browser/readme.md) | The site needs Chrome, JavaScript, or a browser reputation signal. |
| [`capture`](packages/capture/readme.md) | You need to discover a request with your own Chrome profile. |
| [`portal`](packages/portal/readme.md) | People need job submission, teams, reusable results, and a worker fleet. |

Most new sites begin in `capture`, move to `browser` if a browser is part of
the protocol, and move to `fetch` only when the request works without Chrome.
That is a workflow, not a dependency rule. See [Adding a site](docs/adding-a-site.md).

## More

- [Architecture](ARCHITECTURE.md) explains package boundaries and durable state.
- [Site notes](docs/sites/) record current request and response contracts.
- [Operations](docs/operations/) covers deployment, diagnosis, and
  [multi-host fetch jobs](docs/operations/sharded-fetch.md).
- [Results](docs/reports/results.md) contains historical measurements.

## Development

Run workspace checks from the repository root:

```sh
mise run format
mise run check
mise run test
mise run build
```

`mise run dev` starts the disposable local PostgreSQL instance, bootstraps the
portal, and runs the web process. Use `mise run reset` only when that local
database may be removed.
