# System architecture

## System overview

The system takes a CSV of Peruvian identity documents (DNIs and RUCs), looks
them up on public sites, and writes results to output CSVs. It distributes
lookups across concurrent proxy sessions and records all state in a SQLite
database.

```
Input CSV
   ↓
[Planner]: discovers what needs to be looked up
   ↓
[Worker pool per site]
   ├─ Lane 1 (provider session)
   ├─ Lane 2 (provider session)
   └─ Lane N (provider session)
   ↓
[Site-specific logic]
   ├─ ready(): session warmup/health check
   ├─ lookup(): perform the request
   └─ parse(): convert response to result row
   ↓
[State database]: SQLite, source of truth
   ↓
[Export]: CSV outputs and projections
```

Each package implements this flow differently:

- **fetch**: HTTP requests through proxy providers. See
  [packages/fetch/readme.md](../packages/fetch/readme.md).
- **browser**: Chrome over the DevTools protocol, for sites that gate on JS or
  reCAPTCHA/Cloudflare. See
  [packages/browser/readme.md](../packages/browser/readme.md).
- **capture**: same idea as browser, but through your own Chrome profile instead
  of an automated one. See
  [packages/capture/readme.md](../packages/capture/readme.md).
- **portal**: web UI and worker that runs fetch jobs on behalf of a team. See
  [packages/portal/readme.md](../packages/portal/readme.md).

## Job lifecycle

A job follows this sequence: input → planning → execution → state recording →
export → optionally resume.

### 1. Input validation

Read a CSV file containing one identifier per row (7-8 digit DNI, or 11-digit
RUC). Drop empty rows and invalid document numbers. Deduplicating is optional.

The unit of work is a `(doc, site)` pair. If you select multiple sites, each
document is looked up independently on each site.

### 2. Planning

For each `(doc, site)` pair:

- Does this site accept this document type?
- Did a previous run already handle this pair successfully?

If both are yes, skip it (this is how resumption works). Otherwise, queue it for
lookup. Documents accepted by none of the selected sites are ignored and counted
as such.

### 3. Execution

The pipeline runs one worker pool per site inside an async `TaskGroup`. Each
pool claims work from a queue.

A **lane** is one concurrent worker, backed by a provider session:

1. Open a sticky proxy session (credentials from environment)
2. Run the site's `ready()` method to warm up and health-check
3. Claim documents from the queue and look them up
4. On ban or session budget exhaustion, rotate the session
5. On hard error or repeated rejections, retry the document

Each `(site, provider)` pair has its own **circuit breaker**. Ten consecutive
deterministic failures trip it, parking all lanes for that pair until the first
successful document (then it recovers). This prevents a single broken document
from wasting retries on healthy providers.

Provider mechanics (lane allocation, sticky sessions, per-provider tuning) are
documented once, in [proxies.md](proxies.md). Don't restate them here or in a
package readme.

### 4. State recording

Every lookup attempt, success, or failure is recorded in a SQLite database
(`*.state.sqlite3`). The `outcomes` table is the source of truth.

A `(doc, site)` pair is marked:

- `success`: completed successfully
- `not_found`: the site confirmed no result exists
- `error`: terminal failure (retrying won't change the result)
- `rejected`: ambiguous rejection (worth retrying; happens with reCAPTCHA,
  Turnstile)
- `pending`: not yet attempted

A pair receives `MAX_ATTEMPTS` healthy-contact attempts. It retires only after
success or reaching that cap. Attempts made while a provider circuit breaker is
open don't count. An outage can't waste your retry budget.

### 5. Export

When a run ends (success, error, or Ctrl-C), exports run from a `finally` block.
Files are written atomically once, next to `--output`.

Outputs include:

- `out.<site>.csv`: successful rows
- `out.<site>.<projection>.csv`: derived views (computed from stored rows, never
  triggering new requests)
- `out.<site>.errors.csv`: terminal failures
- `out.<site>.not_found.csv`: documents the site confirmed don't exist
- `out.state.sqlite3`: the state database (importable on next run)

During a run, read progress from the state database, not the CSV. Output CSVs
don't exist until the run ends. A mid-run directory with no output is normal,
not a broken run. See [troubleshooting.md](troubleshooting.md) if this looks
like a stall.

### 6. Resume (next run)

Re-running with the same `--output` skips every pair that already succeeded or
reached the retry cap. The state database is loaded automatically.

To rebuild state from previous per-site exports (without a state database), use
`--import` once. To start completely fresh, delete the state database and rerun.

## Key invariants

These rules hold everywhere in the system:

**State database is the source of truth.** CSV outputs are read-only projections
computed from stored rows. Never edit a CSV and expect it to merge back;
instead, update the database and re-export.

**Outputs are disposable.** The CSV exists only for human readability. The state
database is what persists across reruns.

**Each `(doc, site)` pair is independently resumable.** If a run crashes after
50,000 lookups and 0 successes, the next run retries only the remaining pairs.
No batching, no global offset.

**Retry is not infinite.** A pair retires after success or `MAX_ATTEMPTS`
healthy-contact attempts, whichever comes first. Attempts made during circuit
breaker outages don't count.

**Circuit breaker protects against cascading failures.** One failing provider
can't exhaust retries for others. Each `(site, provider)` pair has its own
breaker, and recovery happens on the first success.

**Sessions rotate on ban or budget exhaustion.** A sticky session is reused
until the provider bans it or the session budget is spent (e.g., 50 lookups per
SUNAT session). Then a fresh session starts.

**Do not add cross-package imports.** fetch, browser, and capture each keep
their own copy of a site's parser, columns, and document vocabulary. This
independence lets them evolve separately: a site that works in capture might not
yet work in browser, and might never need to move to fetch. Portal imports fetch
only, and only for types, not runtime logic.

## Retry and resume semantics

### What counts as an attempt?

Any call to the site's `lookup()` method that contacts the provider, even if the
provider circuit breaker was open at the time. Attempts made during outages
don't count.

Rejected lookups (e.g., reCAPTCHA rejection) do count as attempts. They prove
the session is healthy; they're just ambiguous about whether the document
exists.

### How many attempts?

Each package defines `MAX_ATTEMPTS` (usually 4). After that many healthy-contact
attempts, a pair is marked as failed. No more retries.

### Automatic vs. manual retry

**Automatic (next run):** re-running with the same `--output` retries any pair
not yet succeeded. The planner checks the database and skips completed pairs.

**Manual (code change):** if you fix a bug in the site's parser and want to
retry successful pairs, delete the state database and rerun.

### Ambiguous failures (rejections)

Some sites return an ambiguous rejection that doesn't prove the document doesn't
exist: Entel's reCAPTCHA and Portabilidad's Turnstile both do this. See
[sites/entel.md](sites/entel.md) and
[sites/portabilidad.md](sites/portabilidad.md) for what causes each. These are
marked `rejected` in the database and retried with a fresh token or session, on
the same run if retries remain or on the next run otherwise. Only after
`MAX_ATTEMPTS` do they go to `.errors.csv`.

### Terminal failures

A terminal failure proves the document can't be looked up on this site: document
number is invalid, the site confirmed "no result found", or the site returned a
CSRF/session-bootstrap error. These are marked `not_found` or `error` and never
retried, even on a rerun.

## Site-agnostic layers

The planner, pipeline, and storage layers know nothing about any site. They are
reusable across all packages.

Adding a site means:

1. Create `packages/<package>/<package>/sites/<sitename>/` with `page.py` (drive
   the site) and `parse.py` (convert response to result)
2. Add one entry to `sites/registry.py` with site metadata (columns, whether it
   allows empty results, etc.)
3. Each other package (fetch, browser, capture) does the same independently. No
   shared code.

See [adding-a-site.md](adding-a-site.md) for the full workflow, and
[sites/](sites/) for what's already known about each site's wire behavior.

## State database schema

The `outcomes` table in `*.state.sqlite3` is the single source of truth:

| Column         | Meaning                                                       |
| -------------- | ------------------------------------------------------------- |
| `site`         | Site name (e.g., "osiptel", "sunat")                          |
| `doc`          | Document identifier (DNI or RUC)                              |
| `status`       | success, not_found, error, rejected, pending                  |
| `error_code`   | Machine-readable error (e.g., "ban_signal", "not_found")      |
| `error_detail` | Human-readable error message                                  |
| `attempt`      | Which attempt this was (1, 2, 3, ...)                         |
| `session_id`   | Identifier for the provider session used                      |
| `proxy_id`     | Identifier for the provider exit (e.g., "proxy-1-port-10023") |
| `timestamp`    | When this outcome was recorded                                |
| `result_json`  | (optional) Parsed result for successful lookups               |

Query it to understand progress, diagnose failures, or verify reconciliation:

```sql
select status, count(*) from outcomes group by status;
```

## Reconciliation

The sum of all outcomes for a job should equal the input count (accounting for
ignored rows). The exact reconciliation formula differs by site's `allows_empty`
setting; see [sites/osiptel.md](sites/osiptel.md) and
[sites/sunat.md](sites/sunat.md) for the two variants, and
[results.md](results.md) for reconciliation numbers from real jobs.

## See also

- [proxies.md](proxies.md): provider mechanics, lane allocation, tuning
- [troubleshooting.md](troubleshooting.md): runbook for a job that looks broken
- [adding-a-site.md](adding-a-site.md): the capture → browser → fetch workflow
- [results.md](results.md): empirical job data
