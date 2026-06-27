# robot

Bulk-looks-up Peruvian RUC phone-line counts from the OSIPTEL public endpoint
(`checatuslineas.osiptel.gob.pe`). Input is a CSV of RUCs; output is line-count
CSVs. Work is fanned out across concurrent lanes, each routed through its own
sticky GeoNode residential proxy session.

## Setup

```sh
curl https://mise.run | sh   # install mise
mise install                 # toolchain (uv, python, ruff)
uv sync                      # dependencies
```

## Configure

Copy the example env file and fill in your GeoNode credentials:

```sh
cp .env.example .env
```

```ini
GEONODE_USER=<value>
GEONODE_PASS=<value>
GEONODE_GATEWAY=fr            # fr | fr_whitelist | us | sg
GEONODE_TYPE=residential      # residential | datacenter | mix
GEONODE_COUNTRY=PE            # must be PE; see below
GEONODE_LIFETIME=10           # sticky session lifetime, 3..1440 minutes
```

**`GEONODE_COUNTRY` must be `PE`.** OSIPTEL only serves Peruvian residential
exits. Any other country (or an empty value, which means a global pool) is
blocked by the WAF with `status=500`.

## Run

```sh
uv run robot --input rucs.csv --output out.csv --env-file .env
```

That is the whole command. The defaults are tuned for the live endpoint; you
rarely need any other flag.

Outputs (next to `--output`):

- `out.csv` — successes, columns `ruc,carrier,lines,total_lines`.
- `out.errors.csv` — failures, columns `ruc,error_code,error_detail,attempt,session_id,proxy_id,timestamp`.
- `out.state.sqlite3` — the resume database (source of truth). Do not edit it.

Runs are resumable: re-running the same command skips RUCs already succeeded in
the state database. Delete `out.state.sqlite3` to start over. If the state
database does not exist yet, rows from an existing `out.csv` are imported once
so prior results are not re-fetched.

## Tuning

The defaults are the recommended settings. Two knobs matter:

- **`--workers` (default 15)** sets concurrency. Throughput scales nearly
  linearly with it; memory stays flat (~350 MB at 20 workers), so the proxy
  gateway is the limit, not the host. Use 10 for the cleanest run, 20 to
  maximize throughput at the cost of some retryable proxy errors. Worker count
  does not change how many proxy sessions you consume.
- **`--session-budget` (default 1)** caps lookups per proxy session. Leave it
  at 1. OSIPTEL needs a fresh home-page warmup before each lookup, so reusing a
  session for a second API call triggers WAF blocks. A higher budget consumes
  fewer sessions but fails most lookups.

Other flags: `--page-size` (default 5000, usually one request per RUC),
`--ban-cooldown-s` (default 30), `--dedupe/--no-dedupe` (default on),
`--wait-min-s` / `--wait-max-s` (default 0), `--debug`.
