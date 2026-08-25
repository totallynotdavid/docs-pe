# Fetch

Bulk lookup of public Peruvian data over HTTP. Fetch distributes document/site
pairs across proxy sessions, records outcomes in SQLite, and writes CSV
projections.

```sh
uv run --env-file .env fetch \
  --input docs.csv \
  --output out.csv \
  --sites osiptel
```

Fetch is for sites that work over plain HTTP. Use
[browser](../browser/readme.md) when the site needs Chrome or client-side
protection. Start with [capture](../capture/readme.md) when you need to discover
the request.

## Sites

| Site         | Input      | Output                                                          |
| ------------ | ---------- | --------------------------------------------------------------- |
| `osiptel`    | DNI or RUC | Phone lines: `modalidad`, redacted `numero`, `operador`         |
| `sunat`      | RUC-10     | Identity: `tipo_doc`, `num_doc`, `nombre`, `tipo_contribuyente` |
| `sunat_reps` | RUC-20     | Legal representatives: `nombre`, `cargo`, `fecha_desde`         |

The input is a single-column CSV. Seven-digit DNIs are padded to eight digits;
11-digit RUCs are kept as strings. Empty or malformed rows are ignored. Each
`(document, site)` pair is planned and resumed independently.

Wire behavior and reconciliation rules live in the
[site notes](../../docs/sites/).

## Configuration

Set provider credentials in `.env`:

```env
PROXY_PROVIDER=geonode:30,dataimpulse:18
```

`PROXY_PROVIDER` is a comma-separated list of `name[:lanes]`. Provider fields
and lane tuning are documented in [Proxy configuration](../../docs/proxies.md).

## Command-line interface

| Flag                       | Default          | Notes                                                                                                                                                    |
| -------------------------- | ---------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `--dedupe` / `--no-dedupe` | on               | Drop duplicate documents from the input                                                                                                                  |
| `--session-budget`         | site default     | Lookups per sticky session; OSIPTEL=1 (fresh session per lookup), SUNAT=50. A site's own value is a ceiling: this flag can only lower it, never raise it |
| `--ban-cooldown-s`         | provider default | Delay applied after a provider ban                                                                                                                       |
| `--wait-min-s`             | 0                | Minimum delay after a successful lookup                                                                                                                  |
| `--wait-max-s`             | 0                | Maximum delay, sampled uniformly with `--wait-min-s`                                                                                                     |
| `--import`                 | off              | Rebuild state from prior per-site exports before planning                                                                                                |
| `--debug`                  | off              | Log fetch at DEBUG level (`httpx` stays at WARNING)                                                                                                      |

`uv run fetch --help` prints this same table.

## State and output

CSV outputs are written atomically when the run ends. The state database holds
the resumable outcome ledger.

| File                          | Contents                                                                 |
| ----------------------------- | ------------------------------------------------------------------------ |
| `out.<site>.csv`              | Successful rows                                                          |
| `out.<site>.<projection>.csv` | Derived views, e.g. `out.osiptel.counts.csv` for per-carrier line counts |
| `out.<site>.errors.csv`       | Terminal failures and attempt metadata                                   |
| `out.<site>.not_found.csv`    | Documents the site confirmed absent                                      |
| `out.state.sqlite3`           | Resumable outcomes and the run ledger                                    |

Reuse the same output to resume a run. Use `--import` to rebuild state once from
previous exports. Delete the state database, for example `rm out.state.sqlite3`,
to start fresh.

Read [Architecture](../../ARCHITECTURE.md) for lifecycle, retry, and circuit
breaker semantics. Read
[Troubleshooting](../../docs/operations/troubleshooting.md) when output files
and the state database disagree.
