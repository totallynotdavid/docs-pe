# System architecture

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

Each package implements this flow differently: `fetch` makes HTTP requests
through proxy providers (see
[packages/fetch/readme.md](../packages/fetch/readme.md)), `browser` drives
Chrome over the DevTools protocol for sites that gate on JS or
reCAPTCHA/Cloudflare
([packages/browser/readme.md](../packages/browser/readme.md)), `capture` does
the same through your own Chrome profile instead of an automated one
([packages/capture/readme.md](../packages/capture/readme.md)), and `portal` is
the web UI and worker that runs fetch jobs on behalf of a team
([packages/portal/readme.md](../packages/portal/readme.md)). Each package keeps
its own copy of a site's parser, columns, and document vocabulary rather than
sharing code: a site that works in `capture` might not yet work in `browser`,
and might never need to move to `fetch`. Portal imports `fetch`, and only for
types, not runtime logic. This independence is deliberate, and it's the one rule
in this codebase worth never breaking: do not add cross-package imports.

A job moves through input validation, planning, execution, state recording,
export, and resume. Input validation reads a CSV with one identifier per row
(7-8 digit DNI, or 11-digit RUC), drops empty rows and invalid document numbers;
deduplication is optional. The unit of work is a `(doc, site)` pair, so
selecting multiple sites looks up each document independently on each one.
Planning then checks, for each pair, whether the site accepts that document type
and whether a previous run already succeeded on it: if both are true it's
skipped (this is how resumption works), otherwise it's queued. Documents
accepted by none of the selected sites are ignored and counted as such.

Execution runs one worker pool per site inside an async `TaskGroup`, each pool
claiming work from a queue. A lane is one concurrent worker backed by a provider
session: it opens a sticky proxy session from environment credentials, runs the
site's `ready()` method to warm up and health-check, claims documents and looks
them up, rotates its session on ban or budget exhaustion, and retries on hard
error or repeated rejection. Each `(site, provider)` pair has its own circuit
breaker: ten consecutive deterministic failures trips it, parking every lane for
that pair until the next success, so one broken document can't burn retries
meant for healthy providers. Provider mechanics (lane allocation, sticky
sessions, per-provider tuning) are documented once, in [proxies.md](proxies.md);
this file doesn't restate them.

Every attempt, success, or failure is written to a SQLite database
(`*.state.sqlite3`) as it happens; the `outcomes` table is the source of truth,
and a `(doc, site)` pair is marked `success` (completed), `not_found` (the site
confirmed no result exists), `error` (terminal failure, retrying won't change
the result), `rejected` (ambiguous rejection, worth retrying, happens with
reCAPTCHA and Turnstile), or `pending` (not yet attempted). Any call to the
site's `lookup()` method that contacts the provider counts as an attempt, even a
rejected one, since a rejection proves the session is healthy and is just
ambiguous about whether the document exists; attempts made while a provider's
circuit breaker is open don't count, so an outage can't waste retry budget. A
pair retires after success or `MAX_ATTEMPTS` healthy-contact attempts (usually
4), whichever comes first, and never retries again after that, not even on a
rerun, unless it was a `rejected` pair (which keeps getting a fresh token or
session, on the same run if retries remain or the next run otherwise) rather
than a genuinely terminal one. See [sites/entel.md](sites/entel.md) and
[sites/portabilidad.md](sites/portabilidad.md) for what causes each site's
rejections.

When a run ends, by success, error, or Ctrl-C, export runs from a `finally`
block and writes files atomically once next to `--output`: `out.<site>.csv` for
successful rows, `out.<site>.<projection>.csv` for derived views computed from
stored rows (these never trigger new requests), `out.<site>.errors.csv` for
terminal failures, `out.<site>.not_found.csv` for documents the site confirmed
don't exist, and `out.state.sqlite3` itself, importable on the next run. During
a run, read progress from the state database, not the CSV: output CSVs don't
exist until the run ends, and a mid-run directory with no output is normal, not
a broken run (see [troubleshooting.md](troubleshooting.md) if it looks like a
stall). Because every `(doc, site)` pair is independently resumable, re-running
with the same `--output` just skips every pair that already succeeded or reached
the retry cap: no batching, no global offset, so a run that crashes after 50,000
lookups and 0 successes picks up exactly where it left off. Use `--import` once
to rebuild state from previous per-site exports without a state database, or
delete the state database to start completely fresh.

The planner, pipeline, and storage layers know nothing about any site and are
reusable across all packages. Adding a site means creating
`packages/<package>/<package>/sites/<sitename>/` with `page.py` (drive the site)
and `parse.py` (convert response to result), then adding one entry to
`sites/registry.py` with site metadata (columns, whether it allows empty
results, and so on); see [adding-a-site.md](adding-a-site.md) for the full
workflow and [sites/](sites/) for what's already known about each site's wire
behavior.

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

The sum of all outcomes for a job should equal the input count (accounting for
ignored rows), though the exact formula differs by a site's `allows_empty`
setting: see [sites/osiptel.md](sites/osiptel.md) and
[sites/sunat.md](sites/sunat.md) for the two variants, and
[results.md](results.md) for reconciliation numbers from real jobs.
