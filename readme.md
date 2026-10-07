# docs-pe

docs-pe looks up public Peruvian records for a list of identifiers: the phone
lines registered to a DNI or RUC (OSIPTEL), taxpayer identity and legal
representatives (SUNAT), Entel debt, and the current carrier of a mobile number
(Portabilidad). It runs as command-line tools for one operator, or as a web
portal where teams share jobs, results, and a fleet of worker nodes.

The tools reach each site through a residential proxy provider (GeoNode or
DataImpulse). You bring the provider account. Inputs, state databases, and CSV
exports can contain personal data: keep them private, do not commit them, and
use the services only where the collection and use are authorized.

## Get started

Install [mise](https://mise.jdx.dev), then the pinned toolchain and
dependencies:

```sh
mise install
mise run install
```

Put one identifier per row in the first column of a CSV, copy the environment
template, and fill in the credentials of the providers named by
`PROXY_PROVIDER`:

```sh
printf '%s\n' '20131312955' > subjects.csv
cp .env.example .env
```

Look up the phone lines registered to that RUC:

```sh
uv run --env-file .env fetch \
  --input subjects.csv \
  --output results/out.csv \
  --sites osiptel
```

The run writes `results/out.osiptel.csv` with the columns `doc`, `modalidad`,
`numero`, and `operador`, and records every outcome in
`results/out.state.sqlite3`. Check a run, finished or interrupted, with:

```sh
uv run fetch-status --output results/out.csv
```

The [input format](docs/input-format.md) lists the accepted identifiers.

## Features

| Tool                                    | Use it when                                                                                                                          |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| [`fetch`](packages/cli/readme.md)       | The site accepts ordinary HTTP requests and the job needs unattended scale. Resumes from its state database and shards across hosts. |
| [`browser`](packages/browser/readme.md) | The site needs Chrome, JavaScript, or browser reputation.                                                                            |
| [`capture`](packages/capture/readme.md) | You need to discover a request with your own Chrome profile.                                                                         |
| [`portal`](packages/portal/readme.md)   | People need job submission, teams, reusable results, and a worker fleet.                                                             |

Supported sites and the tool that runs each:

| Site                                 | Looks up                                                   | Tool                 |
| ------------------------------------ | ---------------------------------------------------------- | -------------------- |
| [OSIPTEL](docs/osiptel.md)           | Phone lines per DNI or RUC                                 | `fetch`              |
| [SUNAT](docs/sunat.md)               | Natural-person identity and legal-entity representatives   | `fetch`              |
| [Entel](docs/entel.md)               | Debt per DNI or RUC                                        | `browser`, `capture` |
| [Portabilidad](docs/portabilidad.md) | Current carrier and portability history of a mobile number | `browser`            |

## Learn more

- [Documentation](docs/readme.md): the manual, from input format to portal
  deployment.
- [Architecture](docs/architecture.md): packages, boundaries, and the outcome
  model.
- [Contributing](.github/CONTRIBUTING.md): set up, check, and submit a change.
