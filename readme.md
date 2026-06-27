# robot

Bulk lookup of phone-line counts for Peruvian RUCs against the public OSIPTEL
endpoint (`checatuslineas.osiptel.gob.pe`). Reads a CSV of RUCs, writes
per-RUC line counts and a resume state database. Work is fanned out across
concurrent lanes, each routed through a sticky session on a configured proxy
provider.

## Install

```sh
mise install          # toolchain: uv, python, ruff
uv sync               # project + dev dependencies
```

Tasks (run from the repo root):

```sh
mise run format       # ruff format + ruff check --fix
mise run check        # mypy .
mise run test         # pytest
mise run build        # pyinstaller single binary
```

## Configure

Copy `.env.example` to `.env`. `PROXY_PROVIDER` is a comma-separated list and
the order is preserved. Unknown names or duplicates fail at startup.

```ini
PROXY_PROVIDER=geonode,dataimpulse

# GeoNode: per-slot sticky ports (10000..10900); release via API on rotate.
GEONODE_USER=<value>
GEONODE_PASS=<value>
GEONODE_GATEWAY=fr            # fr | fr_whitelist | us | sg
GEONODE_TYPE=residential      # residential | datacenter | mix
GEONODE_COUNTRY=PE            # uppercase, must be PE
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

`GEONODE_COUNTRY=PE` and `DATAIMPULSE_COUNTRY=pe` are required, not optional.
OSIPTEL's WAF blocks foreign exits with `status=500`, and an empty country
silently routes through a global pool. Both providers also need the
corresponding `*_USER` and `*_PASS`.

## Run

```sh
uv run robot --input rucs.csv --output out.csv --env-file .env
```

`--input` is a single-column CSV of 11-digit RUCs. Rows that are empty or
not 11 digits are dropped silently and counted in the run summary under
`ignored`. The other flags default to the live-endpoint settings and rarely
need changing:

| flag              | default | notes |
| ----------------- | ------- | ----- |
| `--page-size`     | 5000    | OSIPTEL returns all rows in one page for any RUC seen in practice |
| `--dedupe`        | on      | collapse duplicate RUCs in the input |
| `--session-budget`| 1       | OSIPTEL rejects a reused session on the second API call |
| `--wait-min-s`    | 0       | optional sleep between successful lookups in a lane |
| `--wait-max-s`    | 0       | upper bound; the wait is uniform in `[min, max]` |
| `--debug`         | off     | `robot.*` loggers at DEBUG, `httpx` and `httpcore` at WARNING |

`--session-budget=1` is a protocol constraint, not a tuning knob. OSIPTEL
requires a fresh home-page warmup per lookup, so a higher budget fails
most lookups while saving only proxy sessions.

## Outputs

Three files are written next to `--output`. All writes are atomic
(`*.tmp` + rename) and happen even on interruption, so the artifacts on
disk always reflect the durable state.

- `out.csv` — successes, columns `ruc,carrier,lines,total_lines`.
- `out.errors.csv` — terminal failures, columns `ruc,error_code,error_detail,attempt,session_id,proxy_id,timestamp`.
- `out.state.sqlite3` — the resume database and source of truth. Do not edit it.

A run is fully resumable. Re-running with the same `--output` skips any RUC
that already has a row in `results` or `errors`. On the very first run, when
the state DB does not exist yet, an existing `out.csv` is imported once so
prior successes are not re-fetched. Delete `out.state.sqlite3` to start
over. A re-run that finally succeeds clears the prior error for that RUC,
and vice versa; each row always reflects the most recent attempt.

Logs go to stdout and to `logs/<run_id>.log` (one file per run, append).
A run summary is emitted at the end with `rows_read`, `valid`, `ignored`,
`duplicates`, `already_done`, `pending`, `processed`, `succeeded`,
`failed`, `remaining`, plus the cumulative counts in the state DB.

## How it works

A run is one shared queue of pending RUCs. Each provider contributes a
fixed number of lanes (`workers=15` for GeoNode, `workers=18` for
DataImpulse) that pull from the queue in any order. A lane opens one
sticky proxy session, warms up the OSIPTEL home page, performs lookups,
and closes the session on rotate or exit.

Sticky sessions are provider-specific but lane-neutral:

- GeoNode: one port per lane slot (`10000 + slot_id - 1`); a fresh random
  `sessionId` is baked into the username on each new session, so a rotation
  forces a new exit IP. Releases are explicit (`PUT
  monitor.geonode.com/sessions/release/proxies`, up to 3 attempts with
  backoff).
- DataImpulse: a single rotating port (`823`); stickiness is the `sessid`
  in the username. No release API; sessions expire by `sessttl`.

The OSIPTEL adapter is a paginated POST to
`/Consultas/GetAllCabeceraConsulta/` with DataTables-style params. For
each page it returns `iTotalRecords` and the rows, from which the adapter
groups counts by `operador`. `page_size=5000` is one request per RUC in
practice. Status classification:

- `403` / `429` and WAF page text (`attack id:`, `web page blocked`, `url
  you requested has been blocked`) are treated as bans; the sticky session
  is rotated and the lane cools down for `ban_cooldown_s` (30s by default).
- `502` / `503` / `504` are treated as upstream degradation; the same
  session retries without rotation.
- A non-JSON body, a missing `iTotalRecords`, a missing `data`/`aaData`
  array, or `estado=true` is a schema error and rotates the session.
- A `httpx.HTTPError` is a transport error and retries on the same
  session.

The retry policy has three independent axes: `retry` (try the same RUC
again), `rotate` (discard the sticky session first), and `cooldown_s`
(pause the lane before the next acquisition). A RUC gets up to
`MAX_ATTEMPTS=4` attempts within one run; beyond that, the failure is
persisted as terminal and the RUC is not retried until the next launch.

Every lane records its egress IP once per session by dialing a public
probe service (`ip-api.com`, `ipify`, `httpbin.org`, in order, with three
rounds) through its proxy. Failures are logged at WARNING and the lookup
proceeds anyway.

The two persisted session identifiers mean different things. `proxy_id`
is the sticky label the provider assigns (`proxy-1-port-10000` for
GeoNode, `dataimpulse-slot-1` for DataImpulse). `session_id` is the
per-open OSIPTEL uuid, regenerated every time the lane opens a new
session. Both are written to the error rows so a single failed RUC can be
traced end to end.

A RUC string is exactly 11 digits; non-conforming rows are dropped from
the plan and counted in `ignored`, not in errors. `--dedupe` collapses
duplicates inside the input CSV; duplicates are counted in `duplicates`.

## Cancellation

`Ctrl-C` and `SIGTERM` both cancel through the same path. The lane
currently in flight finishes its in-progress lookup, the sticky session
is released (or expires by TTL), the state DB is closed, and the final
CSV export runs in `finally` so the artifacts reflect the work that was
recorded.
