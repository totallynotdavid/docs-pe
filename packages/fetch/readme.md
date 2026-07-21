# fetch

Bulk lookup of public data for Peruvian RUCs across one or more public government
sites. Reads a CSV of RUCs and writes one result CSV per site, backed by a durable
resume database. Work is fanned out across concurrent async lanes, each routed
through a sticky session on a configured proxy provider.

Two sites are built in and compose in a single run:

- `osiptel` (`checatuslineas.osiptel.gob.pe`): phone-line counts per carrier.
- `sunat` (`e-consultaruc.sunat.gob.pe`): the "Tipo de Documento" record
  (document type, number, and name) from the RUC consulta page.

The unit of work is a `(ruc, site)` pair, so each site's lookup for a RUC is an
independent, independently-resumable job.

## Install

```sh
mise install          # toolchain: uv, python, ruff
uv sync               # project + dev dependencies
```

Tasks (run from the repo root):

```sh
mise run format       # ruff format + ruff check --fix
mise run check        # mypy .
mise run build        # pyinstaller single binary
```

## Configure

Copy `.env.example` to `.env`. `PROXY_PROVIDER` is a comma-separated list and the
order is preserved. Unknown names or duplicates fail at startup. Every site is
proxied, so a run fails fast if no provider is configured.

```ini
PROXY_PROVIDER=geonode,dataimpulse

# GeoNode: per-slot sticky ports (10000..10900); release via API on rotate.
GEONODE_USER=<value>
GEONODE_PASS=<value>
GEONODE_GATEWAY=fr            # fr | fr_whitelist | us | sg
GEONODE_TYPE=residential      # residential | datacenter | mix
GEONODE_COUNTRY=PE            # uppercase, must be PE for OSIPTEL
GEONODE_STATE=
GEONODE_CITY=
GEONODE_ASN=
GEONODE_STRICT_OFF=false
GEONODE_LIFETIME=10           # minutes, 3..1440

# DataImpulse: stickiness via sessid in the username; sessions expire by sessttl.
DATAIMPULSE_USER=<value>
DATAIMPULSE_PASS=<value>
DATAIMPULSE_COUNTRY=pe        # lowercase ISO-3166, must be pe
DATAIMPULSE_SESSTTL=3         # minutes, >= 1
```

OSIPTEL's WAF blocks foreign exits, so `GEONODE_COUNTRY=PE` / `DATAIMPULSE_COUNTRY=pe`
are required. SUNAT is not IP-bound but is still proxied to spread per-IP rate limits
at scale. Both providers also need their `*_USER` and `*_PASS`.

## Run

```sh
uv run fetch --input rucs.csv --output out.csv --sites sunat,osiptel --env-file .env
```

`--input` is a single-column CSV of 11-digit RUCs (RUC-10 for persons, RUC-20 for
companies). Rows that are empty or not 11 digits are dropped silently and counted
under `ignored`. `--sites` is required. Other flags default to per-site/per-provider
settings and rarely need changing:

| flag              | default        | notes |
| ----------------- | -------------- | ----- |
| `--sites`         | (required)     | comma-separated: `sunat`, `osiptel` |
| `--dedupe`        | on             | collapse duplicate RUCs in the input |
| `--session-budget`| site default   | global override of lookups per sticky session (OSIPTEL 1, SUNAT 50) |
| `--workers`       | provider default | global override of lanes per provider (GeoNode 15, DataImpulse 18) |
| `--ban-cooldown-s`| provider default | global override of the post-ban lane cooldown (30s) |
| `--wait-min-s`    | 0              | optional sleep between successful lookups in a lane |
| `--wait-max-s`    | 0              | upper bound; the wait is uniform in `[min, max]` |
| `--import`        | off            | opt-in: rebuild the store from prior per-site exports before planning |
| `--debug`         | off            | `fetch.*` loggers at DEBUG, `httpx`/`httpcore` at WARNING |

`--session-budget=1` for OSIPTEL is a protocol constraint, not a tuning knob: it
requires a fresh home-page warmup per lookup.

## Outputs

For `--output out.csv` and each requested site, files are written next to `--output`.
All writes are atomic (`*.tmp` + rename) and happen even on interruption, so the
artifacts on disk always reflect the durable state.

- `out.<site>.csv`: successes. Columns are the site's own:
  - `osiptel`: `ruc,carrier,lines,total_lines`
  - `sunat`: `ruc,tipo_doc,num_doc,nombre`
- `out.<site>.errors.csv`: terminal failures, columns
  `ruc,error_code,error_detail,attempt,session_id,proxy_id,timestamp`.
- `out.state.sqlite3`: the resume database and source of truth. Do not edit it.

A successful lookup with no data (an OSIPTEL RUC with no lines, a SUNAT company with
no document row) is an honest success stored with an empty payload: the pair is done
but contributes no CSV rows.

A run is fully resumable. Re-running with the same `--output` skips any `(ruc, site)`
pair that already succeeded or has retired at the retry cap. The state DB is the only
durable artifact; delete `out.state.sqlite3` to start over. To rebuild a lost DB from
its exports, run once with `--import`. A re-run that finally succeeds clears the prior
error for that pair, and vice versa; each row reflects the most recent attempt.

Logs go to stdout and to `logs/<run_id>.log` (one file per run, append). A per-site
summary and a run summary are emitted at the end with `rows_read`, `valid`, `ignored`,
`duplicates`, `pending`, `processed`, `succeeded`, `failed`, plus the cumulative
counts in the state DB.

## How it works

A run plans pending `(ruc, site)` pairs, then launches a worker pool per site under one
async `TaskGroup`. Each provider contributes its configured number of lanes (see
`--workers` above) that drain that site's queue. A lane opens one sticky proxy session,
runs the site's `ready` warmup, performs lookups, and rotates the session on ban or when
`session_budget` is reached.

A **site** is just a value: a name, its output columns, its tuning, and two functions:
`ready(client)` (warm a fresh proxy-bound client) and `lookup(client, ruc) -> rows`
(perform the request(s), parse, return rows, raising the shared error taxonomy). The
pipeline, store, and proxy code are entirely site-agnostic; adding a site is one new
`sites/<name>/` module plus one entry in the `SITES` dict.

Sticky sessions are provider-specific but lane-neutral:

- GeoNode: one port per lane slot (`10000 + slot - 1`, allocated per provider across all
  sites so ports never collide); a fresh random `sessionId` in the username forces a new
  exit IP on rotation. Releases are explicit (`PUT monitor.geonode.com/...`, 3 attempts).
- DataImpulse: a single rotating port (`823`); stickiness is the `sessid` in the username.
  No release API; sessions expire by `sessttl`.

Site protocols:

- **OSIPTEL** is a paginated POST to `/Consultas/GetAllCabeceraConsulta/` with
  DataTables-style params, grouping counts by `operador`. `403`/`429` and WAF page text
  are bans (rotate + cooldown); `502`/`503`/`504` are upstream degradation (retry);
  non-JSON, missing `iTotalRecords`/`data`, or `estado=true` is a schema error.
- **SUNAT** is a single POST to `jcrS00Alias` with `accion=consPorRuc`, the RUC, and a
  random 52-char token (its reCAPTCHA is a client-side stub the server does not verify).
  The result HTML is parsed for the "Tipo de Documento" row. An error page is a transient
  failure; a result page with no document row is a valid empty success.

Retry classification has one owner (`domain/policy.py`): each fault maps to a retry
action and an optional cooldown. A pair gets up to `MAX_ATTEMPTS=4` attempts within one
run; beyond that the failure is persisted, and it retires permanently once its cumulative
healthy-contact attempts cross the cap (attempts made while the provider's breaker is open
do not count, so no outage can grind a valid pair to terminal).

Every lane records its egress IP once per session by dialing a public probe service
(`ip-api.com`, `ipify`, `httpbin.org`, in order). `proxy_id` is the provider's sticky
label; `session_id` is the per-open uuid. Both are written to error rows for tracing.

## Cancellation

`Ctrl-C` and `SIGTERM` both cancel through the same path. The in-flight lookup finishes,
the sticky session is released (or expires by TTL), the state DB is closed, and the final
per-site CSV export runs in `finally` so the artifacts reflect the work that was recorded.
