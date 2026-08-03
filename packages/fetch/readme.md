# fetch

Bulk lookup of public data for Peruvian identity documents. Reads documents from
CSV, distributes lookups across concurrent sticky proxy sessions, and writes one
result CSV per site backed by a resumable state database.

```sh
uv run fetch --input docs.csv --output out.csv --sites sunat,osiptel --env-file .env
```

Sites that work over plain HTTP belong here. Sites that still require a browser
live in [`browser`](../browser/readme.md).

## Sites

| Site         | Accepts                 | Returns                                                                                                   |
| ------------ | ----------------------- | --------------------------------------------------------------------------------------------------------- |
| `osiptel`    | any document            | one row per registered phone line: `modalidad`, redacted `numero`, `operador`                             |
| `sunat`      | RUC-10 (natural person) | `tipo_doc`, `num_doc`, `nombre`, `tipo_contribuyente`                                                     |
| `sunat_reps` | RUC-20 (entity)         | one row per legal representative: `razon_social`, `doc_type`, `num_doc`, `nombre`, `cargo`, `fecha_desde` |

Sites compose in one run. The unit of work is a `(doc, site)` pair, so each
lookup is independently resumable.

Each site declares which document kinds it accepts. Documents accepted by none
of the selected sites appear in no output and are counted as ignored.

### osiptel

`checatuslineas.osiptel.gob.pe`. A paginated POST using DataTables-style
parameters, with `IdTipoDoc` set to 1 for DNI and 2 for RUC. Each page returns
at most 5,000 rows.

The WAF returns `status=500` and a block page for foreign exits. Suspicious
exits may receive HTTP 200 with a CAPTCHA wall, so readiness checks the response
body for a success marker instead of trusting the status code.

[Peru exits are mandatory](../../docs/proxies.md#osiptel-requires-peru-exits).

A document with no phone lines is a valid empty result. Roughly 4% to 30% of a
DNI job may be empty depending on province; see
[the results ledger](../../docs/results.md).

### sunat

`e-consultaruc.sunat.gob.pe`. A single POST to `jcrS00Alias` with
`accion=consPorRuc`, the RUC, and a random 52-character token. SUNAT's reCAPTCHA
wrapper is client-side only: the server checks that a token is present and
plausibly shaped, not that it is genuine.

Two response shapes require special handling:

- A **sucesión indivisa** has no "Tipo de Documento" block. These rows use an
  empty `tipo_doc` and `num_doc`, and take `nombre` from the RUC row. They are
  about 0.1% of a RUC-10 job and do not produce a DNI for OSIPTEL follow-up. A
  missing document block for any other contributor type is treated as parser
  drift.
- An unknown RUC returns a normal result page containing "El número de RUC N
  consultado no es válido". This is terminal `not_found`; retrying cannot change
  the result.

`tipo_doc` is not always `DNI`. One 235,233-row job contained 235,003 DNI, 10
CE, and 1 C. FFPP. Build OSIPTEL follow-ups with `tipo_doc == "DNI"`, not by
checking `num_doc` length.

### sunat_reps

Returns the legal representatives of a RUC-20 entity. A separate JSON identity
request determines whether the representatives request should run.

Some entities, including associations and educational centres, have no listed
representatives. This is a valid empty result.

## Configure

Copy `.env.example` to `.env`. Every site uses a proxy, so startup fails when no
provider is configured.

```ini
PROXY_PROVIDER=geonode:30,dataimpulse:18

GEONODE_USERNAME=<value>
GEONODE_PASSWORD=<value>
GEONODE_GATEWAY=fr              # fr | fr_whitelist | us | sg
GEONODE_PROXY_TYPE=residential  # residential | datacenter | mix
GEONODE_COUNTRY=PE              # uppercase, must be PE for OSIPTEL
GEONODE_LIFETIME_MINUTES=10     # 3..1440

DATAIMPULSE_USERNAME=<value>
DATAIMPULSE_PASSWORD=<value>
DATAIMPULSE_COUNTRY=pe          # lowercase ISO-3166
DATAIMPULSE_SESSION_MINUTES=3   # >= 1
```

`PROXY_PROVIDER` is an ordered, comma-separated list of `name[:lanes]`. Unknown
names and duplicates fail at startup. Lanes are created per provider, so the
example above creates 48 lanes. Omitting `:lanes` uses the provider default.

Provider fields follow `<PROVIDER>_<FIELD>` and are defined by each provider's
`Field` schema in `fetch/proxy/base.py`. The same schema validates environment
variables, stored credentials, and the portal form. Adding a provider requires
one module and one entry in `fetch/proxy/registry.py`.

Provider suitability differs by site. See [proxies.md](../../docs/proxies.md).

## Run

`--input` is a single-column CSV containing:

- 7- or 8-digit DNIs; 7-digit values are padded to 8 digits
- 11-digit RUCs; RUC-10 for persons and RUC-20 for entities

Kinds may be mixed and are detected per row. Empty or invalid rows are dropped
and counted under `ignored`.

Documents are strings, never integers. Roughly 30% of DNIs begin with zero.

`--sites` is required. Other options use site or provider defaults:

| flag               | default          | notes                                                        |
| ------------------ | ---------------- | ------------------------------------------------------------ |
| `--sites`          | required         | comma-separated: `sunat`, `sunat_reps`, `osiptel`            |
| `--dedupe`         | on               | collapse duplicate documents in the input                    |
| `--session-budget` | site default     | lookups per sticky session; OSIPTEL 1, SUNAT 50              |
| `--ban-cooldown-s` | provider default | cooldown after a ban                                         |
| `--wait-min-s`     | 0                | minimum optional delay after a successful lookup             |
| `--wait-max-s`     | 0                | maximum delay; sampled uniformly from `[min, max]`           |
| `--env-file`       | `.env`           |                                                              |
| `--import`         | off              | rebuild state from previous per-site exports before planning |
| `--debug`          | off              | `fetch.*` at DEBUG; `httpx` and `httpcore` remain at WARNING |

Lane count is configured in `PROXY_PROVIDER`, not through a CLI flag.

OSIPTEL requires `--session-budget=1` because each lookup needs a fresh
home-page warmup.

## Outputs

Files are written next to `--output`, once per selected site. Exports are atomic
and run from a `finally` block, including after `Ctrl-C` or `SIGTERM`.

| File                          | Contents                                                                               |
| ----------------------------- | -------------------------------------------------------------------------------------- |
| `out.<site>.csv`              | successful rows; first column is `doc`, followed by site columns                       |
| `out.<site>.<projection>.csv` | derived views over the same stored rows                                                |
| `out.<site>.errors.csv`       | terminal failures: `doc,error_code,error_detail,attempt,session_id,proxy_id,timestamp` |
| `out.<site>.not_found.csv`    | documents for which the site confirmed no result                                       |
| `out.state.sqlite3`           | resume database and source of truth                                                    |

OSIPTEL also exports `out.osiptel.counts.csv` with
`doc,carrier,lines,total_lines`. Projections are computed from stored rows and
never trigger another request. They must agree with the main CSV.

Output CSVs are created when the run ends. During a run, read progress from the
state database:

```sh
uv run python -c "
import sqlite3
from pathlib import Path
from fetch.store.outcomes import state_path_for_output
c = sqlite3.connect(str(state_path_for_output(Path('out.csv'))))
print(c.execute('select status, count(*) from outcomes group by status').fetchall())"
```

Exports use CRLF. The trailing `\r` remains attached to the last field, so
remove it before comparing, joining, or diffing:

```sh
tr -d '\r'
```

Because OSIPTEL allows empty results, the reconciliation rule is:

```text
documents with >= 1 line + documents with 0 lines + terminal failures = input rows
```

Completed examples are recorded in [results.md](../../docs/results.md).

## Resuming

Re-running with the same `--output` skips every `(doc, site)` pair that already
succeeded or reached the retry cap. Delete the state database to start over, or
use `--import` once to rebuild it from prior exports.

A pair receives `MAX_ATTEMPTS` healthy-contact attempts. It retires only after
success or after reaching that cap. Attempts made while a provider circuit
breaker is open do not count, so an outage cannot retire valid work.

`fetch/domain/policy.py` owns retry classification and fault-to-action mapping.

## How it works

The planner builds pending `(doc, site)` pairs and starts one worker pool per
site inside an async `TaskGroup`. Each provider contributes its configured lanes
to each site queue.

A lane:

1. opens a sticky proxy session
2. runs the site's readiness warmup
3. performs lookups
4. rotates after a ban or when `session_budget` is exhausted

A site defines:

- its name and output columns
- an `accepts(doc)` predicate
- tuning and optional projections
- `ready(client, site)` for session warmup
- `lookup(client, doc)` for the actual request

The pipeline, store, and proxy layers are site-agnostic. Adding a site requires
one `sites/<name>/` module and one entry in `sites/registry.py`.

A proxy provider defines a name, `Field` schema, tuning, `normalize`, and
`build`.

Sticky sessions differ by provider:

- GeoNode assigns one port per lane slot, starting at `10000`. Ports are
  allocated across all sites. A random `sessionId` in the username rotates the
  exit, and sessions are explicitly released.
- DataImpulse uses one rotating port and stores stickiness in the username's
  `sessid`. Sessions expire by TTL and have no release call.

Each `(site, provider)` pair has its own circuit breaker, so one failing
provider does not stop healthy providers for that site.

Each lane records its exit IP once per session. `proxy_id` identifies the
provider session and `session_id` identifies the individual open. Both are
stored with failures for attribution.
