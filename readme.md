# docs-pe

Command-line and browser tools for looking up public Peruvian identity,
telephone, taxpayer, and number-portability data.

## Get started

Install the pinned toolchain and dependencies:

```sh
mise install
mise run install
```

Create a CSV whose first column contains identifiers. Replace the example before
running a lookup:

```sh
printf '%s\n' '12345678' > subjects.csv
cp .env.example .env
```

Add proxy credentials to `.env`, then run a small lookup:

```sh
uv run --env-file .env fetch \
  --input subjects.csv \
  --output results/out.csv \
  --sites osiptel
```

Inspect a completed or interrupted run with:

```sh
uv run fetch-status --output results/out.csv
```

## Choose a runner

| Tool | Use it when |
| --- | --- |
| [`fetch`](packages/cli/readme.md) | The site accepts ordinary HTTP requests and the job needs unattended scale. |
| [`browser`](packages/browser/readme.md) | The site needs Chrome, JavaScript, or a browser reputation signal. |
| [`capture`](packages/capture/readme.md) | You need to discover a request with your own Chrome profile. |
| [`portal`](packages/portal/readme.md) | People need job submission, teams, reusable results, and a worker fleet. |

See [Documentation](docs/readme.md) for site notes and operations. Contributors
should read [Contributing](CONTRIBUTING.md).
