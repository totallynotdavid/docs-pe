# docs-pe

Command-line, browser, and portal tools for looking up public Peruvian identity,
telephone, taxpayer, and number-portability data.

Inputs, state databases, and CSV exports can contain personal data. Keep them
private, do not commit them, and use the services only where the collection and
use are authorized.

Choose the smallest runner that matches the site's protocol. `fetch` handles
ordinary HTTP at unattended scale, `browser` handles Chrome and browser gates,
`capture` discovers requests in your own profile, and the portal coordinates
shared jobs, teams, results, and worker nodes.

## Get started

Install the pinned toolchain and dependencies:

```sh
mise install
mise run install
```

Create a CSV whose first column contains identifiers, copy the environment
template, and add proxy credentials to `.env`. See the
[input format](docs/input-format.md) for accepted forms and normalization:

```sh
printf '%s\n' '12345678' > subjects.csv
cp .env.example .env
```

Run a small OSIPTEL lookup:

```sh
uv run --env-file .env fetch \
  --input subjects.csv \
  --output results/out.csv \
  --sites osiptel
```

Inspect a completed or interrupted run from its SQLite state database:

```sh
uv run fetch-status --output results/out.csv
```

See [Documentation](docs/readme.md) for the state model, troubleshooting, output
files, and other task guides.

## Choose a runner

| Tool | Use it when |
| --- | --- |
| [`fetch`](packages/cli/readme.md) | The site accepts ordinary HTTP requests and the job needs unattended scale. |
| [`browser`](packages/browser/readme.md) | The site needs Chrome, JavaScript, or browser reputation. |
| [`capture`](packages/capture/readme.md) | You need to discover a request with your own Chrome profile. |
| [`portal`](packages/portal/readme.md) | People need job submission, teams, reusable results, and a worker fleet. |

See [Documentation](docs/readme.md) for the full task index. Contributors
should read [Contributing](CONTRIBUTING.md).
