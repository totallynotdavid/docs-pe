# Fetch

Bulk lookup of public data for Peruvian identity documents over HTTP. Reads a
CSV, distributes lookups across concurrent proxy sessions, and writes result
CSVs backed by a resumable state database.

```sh
uv run --env-file .env fetch --input docs.csv --output out.csv --sites osiptel
```

## When to use this

Sites that answer over plain HTTP. Sites requiring JavaScript, reCAPTCHA, or a
real Chrome profile stay in [browser](../browser/readme.md). Once a site works
reliably over HTTP, fetch is the workhorse: faster, simpler, and less
infrastructure than browser automation. See
[docs/adding-a-site.md](../../docs/adding-a-site.md) for how a site gets here.

## Supported sites

| Site         | Accepts                  | Returns                                                          |
| ------------ | ------------------------ | ---------------------------------------------------------------- |
| `osiptel`    | any document             | phone lines: `modalidad`, redacted `numero`, `operador`          |
| `sunat`      | RUC-10 (natural persons) | identity: `tipo_doc`, `num_doc`, `nombre`, `tipo_contribuyente`  |
| `sunat_reps` | RUC-20 (entities)        | legal reps: one row per person, `nombre`, `cargo`, `fecha_desde` |

The unit of work is a `(doc, site)` pair, independently resumable. Select
multiple sites in one run; they're queued independently. Wire protocol, failure
modes, and reconciliation rules for each site:
[docs/sites/osiptel.md](../../docs/sites/osiptel.md),
[docs/sites/sunat.md](../../docs/sites/sunat.md).

## Command-line interface

```sh
uv run --env-file .env fetch [options]
```

| Flag               | Default          | Notes                                                                   |
| ------------------ | ---------------- | ----------------------------------------------------------------------- |
| `--input`          | required         | Single-column CSV: 7-8 digit DNI or 11-digit RUC per row                |
| `--output`         | required         | Base filename; outputs written as `out.<site>.csv`, `out.state.sqlite3` |
| `--sites`          | required         | Comma-separated: `osiptel`, `sunat`, `sunat_reps`                       |
| `--dedupe`         | on               | Drop duplicate documents in input                                       |
| `--session-budget` | site default     | Lookups per sticky session (OSIPTEL=1, SUNAT=50)                        |
| `--ban-cooldown-s` | provider default | Delay after provider ban                                                |
| `--wait-min-s`     | 0                | Minimum delay after successful lookup                                   |
| `--wait-max-s`     | 0                | Maximum delay; sampled uniformly                                        |
| `--import`         | off              | Rebuild state from previous exports before planning                     |
| `--debug`          | off              | Log at DEBUG level for fetch (httpx stays at WARNING)                   |

Lane count is set in `PROXY_PROVIDER`, not a flag (see
[docs/proxies.md](../../docs/proxies.md)). OSIPTEL requires `--session-budget=1`
because each lookup needs a fresh session warmup.

## Configuration

Set proxy credentials in `.env`; every site uses at least one provider.

```env
PROXY_PROVIDER=geonode:30,dataimpulse:18
```

`PROXY_PROVIDER` is comma-separated `name[:lanes]`. Unknown names or duplicates
fail at startup; omitting `:lanes` uses the provider default. Provider
credentials (`GEONODE_*`, `DATAIMPULSE_*`), lane tuning, and per-site provider
selection: [docs/proxies.md](../../docs/proxies.md).

## Input

A single-column CSV containing 7-8 digit DNIs (7-digit values are padded to 8),
11-digit RUCs, or a mix of both. Kind is detected per row. Empty or malformed
rows are dropped and counted as ignored. Documents are strings, not integers;
roughly 30% of DNIs begin with zero.

## Outputs and state

Files are written atomically once when the run ends (success, error, or Ctrl-C).

| File                          | Contents                                                                               |
| ----------------------------- | -------------------------------------------------------------------------------------- |
| `out.<site>.csv`              | Successful rows                                                                        |
| `out.<site>.<projection>.csv` | Derived views (computed from stored rows, never trigger new requests)                  |
| `out.<site>.errors.csv`       | Terminal failures: `doc,error_code,error_detail,attempt,session_id,proxy_id,timestamp` |
| `out.<site>.not_found.csv`    | Documents the site confirmed don't exist                                               |
| `out.state.sqlite3`           | State database; the source of truth                                                    |

OSIPTEL also exports `out.osiptel.counts.csv` with per-carrier line counts.

Exports use CRLF line endings: remove `\r` before diffing or comparing
(`tr -d '\r'`).

**During a run**, read progress from the state database, not the CSV (which
doesn't exist yet): see
[docs/troubleshooting.md](../../docs/troubleshooting.md#check-the-state-database-not-logs).

## Resume behavior

Re-running with the same `--output` skips every `(doc, site)` pair that
succeeded or reached the retry cap. To rebuild state from previous exports
without a state database, use `--import` once. To start fresh, delete the state
database (`rm results.state.sqlite3`).

Full retry/resume semantics, circuit breaker behavior, and lane/sticky-session
mechanics: [docs/architecture.md](../../docs/architecture.md).

## Troubleshooting

[docs/troubleshooting.md](../../docs/troubleshooting.md): circuit breaker false
alarms, 407s, port exhaustion, reading an active run.
