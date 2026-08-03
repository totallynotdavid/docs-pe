# fetch

Bulk lookup of public data for Peruvian identity documents. Reads a CSV of
documents, fans the work across concurrent async lanes behind sticky proxy
sessions, and writes one result CSV per site, backed by a durable resume
database.

This is the workhorse of the repo. If a site answers a plain HTTP request, it
belongs here.

```sh
uv run fetch --input docs.csv --output out.csv --sites sunat,osiptel --env-file .env
```

## Sites

| Site         | Accepts                 | Returns                                                                                                   |
| ------------ | ----------------------- | --------------------------------------------------------------------------------------------------------- |
| `osiptel`    | any document            | one row per registered phone line: `modalidad`, redacted `numero`, `operador`                             |
| `sunat`      | RUC-10 (natural person) | `tipo_doc`, `num_doc`, `nombre`, `tipo_contribuyente`                                                     |
| `sunat_reps` | RUC-20 (entity)         | one row per legal representative: `razon_social`, `doc_type`, `num_doc`, `nombre`, `cargo`, `fecha_desde` |

Sites compose in a single run. The unit of work is a `(doc, site)` pair, so each
site's lookup for a document is independent and independently resumable.

Each site declares which documents it accepts, and the planner routes only what
a site can serve. A document no selected site can serve falls out of every
output and is counted so the gap stays visible.

### osiptel

`checatuslineas.osiptel.gob.pe`. A paginated POST with DataTables-style
parameters and `IdTipoDoc` set from the document kind (1 for a DNI, 2 for a
RUC). Each page caps at 5000 rows.

The WAF answers a foreign exit with `status=500` and a block page, and a
suspicious one with HTTP 200 carrying a CAPTCHA wall. The readiness check
therefore looks for a success marker in the body and ignores the status.
[Peru exits are mandatory](../../docs/proxies.md#peru-exits-are-mandatory-for-osiptel).

A document with no phone lines is a real success that contributes no rows.
Expect roughly 4% to 30% of a DNI job, varying by province; see
[the ledger](../../docs/results.md).

### sunat

`e-consultaruc.sunat.gob.pe`. A single POST to `jcrS00Alias` with
`accion=consPorRuc`, the RUC, and a random 52-character token. SUNAT's reCAPTCHA
wrapper is a client-side stub: the server checks only that a token is present
and plausibly shaped, never that it is real.

Two response shapes are answers, and both read as parser drift on first contact:

- **A sucesión indivisa** (the estate of someone who died intestate, which SUNAT
  registers as a taxpayer and taxes like a natural person) has no "Tipo de
  Documento" block at all. Those rows carry an empty `tipo_doc` and `num_doc`,
  and take `nombre` from the RUC row instead. They are around 0.1% of a RUC-10
  job and cluster in the low RUC ranges, so a sorted shard "a" collects nearly
  all of them. They yield a name but no DNI, so they drop out of any OSIPTEL
  follow-up. A missing document block for any _other_ contributor type is still
  drift, and still errors.
- **A RUC that SUNAT has no record of** comes back as a normal result page
  reading "El número de RUC N consultado no es válido". That is `not_found` and
  terminal: the RUC will never resolve, so retrying only burns attempts.

`tipo_doc` is not always `DNI`. One 235,233-row job held 235,003 DNI (8 digits),
10 CE (carné de extranjería, 9 digits) and 1 C. FFPP (6 digits). Filter on
`tipo_doc == "DNI"` when building an OSIPTEL follow-up, never on `num_doc`
length.

### sunat_reps

The legal-representatives table for an entity, plus a separate JSON identity
endpoint that gates whether the reps request runs at all. Some entities
(associations, educational centres) carry no representative in SUNAT's records,
which is a valid empty result.

## Configure

Copy `.env.example` to `.env`. Every site is proxied, so a run fails at startup
if no provider is configured.

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
names and duplicates fail at startup. **Lanes spawn per provider**, so the line
above is 48 lanes, not 30. Omit `:lanes` to take that provider's own tuned
default.

Each variable name is `<PROVIDER>_<FIELD>`, derived from that provider's `Field`
schema in `fetch/proxy/base.py`. The same schema renders the portal's credential
form and validates a stored credential, so adding a vendor is one module plus
one line in `fetch/proxy/registry.py`.

Provider choice is a property of the site, and it differs across the three. See
[proxies.md](../../docs/proxies.md).

## Run

`--input` is a single-column CSV of documents: 7 or 8 digit DNIs (7-digit ones
are zero-padded to the canonical 8) and 11-digit RUCs (RUC-10 for persons,
RUC-20 for entities). Kinds mix freely and are detected per row. Rows that are
empty or neither shape are dropped silently and counted under `ignored`.

Documents are text, never integers. Around 30% of DNIs carry a leading zero.

`--sites` is required. Everything else defaults to a per-site or per-provider
setting and rarely needs changing:

| flag               | default          | notes                                                         |
| ------------------ | ---------------- | ------------------------------------------------------------- |
| `--sites`          | required         | comma-separated: `sunat`, `sunat_reps`, `osiptel`             |
| `--dedupe`         | on               | collapse duplicate documents in the input                     |
| `--session-budget` | site default     | lookups per sticky session (OSIPTEL 1, SUNAT 50)              |
| `--ban-cooldown-s` | provider default | post-ban lane cooldown                                        |
| `--wait-min-s`     | 0                | optional sleep between successful lookups in a lane           |
| `--wait-max-s`     | 0                | upper bound; the wait is uniform in `[min, max]`              |
| `--env-file`       | `.env`           |                                                               |
| `--import`         | off              | rebuild the store from prior per-site exports before planning |
| `--debug`          | off              | `fetch.*` loggers at DEBUG, `httpx`/`httpcore` at WARNING     |

There is no flag for lane count. It is per provider and lives in
`PROXY_PROVIDER`.

`--session-budget=1` for OSIPTEL is a protocol constraint: the site requires a
fresh home-page warmup per lookup.

## Outputs

Written next to `--output`, per requested site. All writes are atomic and happen
in a `finally` block, including on `Ctrl-C` and `SIGTERM`, so the artifacts
always reflect the durable state.

| File                          | Contents                                                                               |
| ----------------------------- | -------------------------------------------------------------------------------------- |
| `out.<site>.csv`              | successes; first column is always `doc`, the rest are the site's                       |
| `out.<site>.<projection>.csv` | a derived view over the same stored rows                                               |
| `out.<site>.errors.csv`       | terminal failures: `doc,error_code,error_detail,attempt,session_id,proxy_id,timestamp` |
| `out.<site>.not_found.csv`    | documents the site answered for with a confirmed absence                               |
| `out.state.sqlite3`           | the resume database and source of truth                                                |

OSIPTEL exports one projection, `out.osiptel.counts.csv`
(`doc,carrier,lines,total_lines`). A projection is a pure function over rows
already stored, materialized at export, so it never costs a second fetch and
must agree with the main CSV exactly. That makes it a cheap integrity check.

**The output CSVs do not exist until the run ends.** A mid-run directory with no
`*.osiptel.csv` is normal and is not evidence of a problem. Read progress from
the state database, never from the CSV or a log tail:

```sh
uv run python -c "
import sqlite3
from pathlib import Path
from fetch.store.outcomes import state_path_for_output
c = sqlite3.connect(str(state_path_for_output(Path('out.csv'))))
print(c.execute('select status, count(*) from outcomes group by status').fetchall())"
```

Exported CSVs use CRLF, and the `\r` rides on the last field, so any comparison
that touches a last field fails on data that is actually identical. Pipe through
`tr -d '\r'` before diffing or joining.

### Reconciling a finished job

Because `osiptel` allows an empty result, unique documents in the main CSV are
always fewer than the number that succeeded. The identity to check is:

```
documents with >= 1 line  +  documents with 0 lines  +  terminal failures  =  input rows
```

Worked examples for every completed job are in
[results.md](../../docs/results.md).

## Resuming

A run is fully resumable. Re-running with the same `--output` skips any
`(doc, site)` pair that already succeeded or retired at the retry cap. The state
database is the only durable artifact; delete it to start over, or run once with
`--import` to rebuild a lost one from its exports.

A pair gets `MAX_ATTEMPTS` tries within a single run. It retires permanently
only by succeeding, or once its cumulative _healthy-contact_ attempts cross the
cap. Attempts made while the provider's circuit breaker was open do not count,
so an outage cannot grind a valid pair to terminal. Every fault is treated as
environmental, so nothing else retires a pair.

`fetch/domain/policy.py` owns that rule and the fault-to-action mapping. Keep
classification there.

## How it works

A run plans pending `(doc, site)` pairs, then launches a worker pool per site
under one async `TaskGroup`. Each provider contributes its configured lanes to
each site's queue. A lane opens one sticky proxy session, runs the site's
`ready` warmup, performs lookups, and rotates the session on a ban or when
`session_budget` is reached.

A **site** is a value: a name, its columns, an `accepts(doc)` predicate, its
tuning, optional projections, and two functions. `ready(client, site)` warms a
fresh proxy-bound client; `lookup(client, doc)` returns rows aligned to
`columns`, raising the shared error taxonomy on failure. The pipeline, store,
and proxy code are entirely site-agnostic, so adding a site is one new
`sites/<name>/` module plus one entry in `sites/registry.py`.

A **proxy provider** mirrors that shape: a name, a `Field` schema, tuning, and
two functions. `normalize` validates raw strings from any source, `build`
returns a live provider.

Sticky sessions are provider-specific but lane-neutral:

- **GeoNode** takes one port per lane slot (`10000 + slot - 1`), allocated per
  provider across all sites so ports never collide. A fresh random `sessionId`
  in the username forces a new exit IP on rotation, and releases are explicit.
- **DataImpulse** uses a single rotating port, with stickiness carried by a
  `sessid` in the username. There is no release call; sessions expire by TTL.

One circuit breaker exists per `(site, provider)`, so a provider-wide outage
parks that site's lanes without stalling a healthy sibling.

Every lane records its egress IP once per session against a public probe
service. `proxy_id` is the provider's sticky label and `session_id` is the
per-open uuid. Both are written to error rows for tracing, and both are needed
to attribute a failure to a vendor.

## Cancellation

`Ctrl-C` and `SIGTERM` cancel through the same path. The in-flight lookup
finishes, the sticky session is released or left to expire, the state database
is closed, and the final export runs. Stopping a run loses nothing: every
outcome is committed as it lands.
