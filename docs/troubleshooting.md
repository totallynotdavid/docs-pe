# Troubleshooting

Start with `fetch-status` or the portal database for the run you are
investigating. Read [Architecture](architecture.md#outcome-state) for the state
model, [Proxy configuration](proxies.md) for provider behavior, and the site
notes in the [index](readme.md) for site-specific failures.

## Inspect a standalone run

Pass the same output path used by `fetch`. The state database is that path with
`.state.sqlite3` in place of its extension, so `results/out.csv` has state in
`results/out.state.sqlite3`:

```sh
uv run fetch-status --output results/out.csv
uv run fetch-status --output results/out.csv --minutes 10
```

The command reports outcomes, the recorded provider, breaker state, and recent
activity with `--minutes`. A failed row may still be retryable.

Progress comes from this database. The CSV files are written when a run ends or
is interrupted, so none exists mid-run, and a run's logs show attempts rather
than outcomes.

## Query the state database

Use SQLite against the state path when a script is not enough:

```sh
sqlite3 results/out.state.sqlite3 \
  "select status, count(*) from outcomes group by status;"
```

If the host does not provide the SQLite CLI, use a short script through the
development environment. Do not use CSV row counts as progress.

The table holds one row per site and document: its latest outcome and the
cumulative `attempt_count`. To inspect one document:

```sql
select site, doc, status, attempt_count, error_code, proxy_id, finished_at
from outcomes
where doc = '12345678'
order by site;
```

## Distinguish failure classes

Check the error code, site, provider, and attempt sample together:

| Signal                                                                          | Likely boundary                                                                                         |
| ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `transport_error` with `407` in `error_detail`, across many documents and lanes | Provider credentials or account capacity.                                                               |
| `ban_signal`                                                                    | The site rejected the exit or returned a block page (HTTP 403, 429, or a 5xx other than 502, 503, 504). |
| `upstream_not_ready` with connection failures                                   | Provider endpoint or upstream availability.                                                             |
| `parse_error` on a small set of documents                                       | Document data or site response shape.                                                                   |
| `provider_schema_error`                                                         | The site response does not match the shape the parser expects.                                          |
| `unknown_error`                                                                 | An exception escaped the known fault taxonomy and needs investigation.                                  |

[`domain/policy.py`](../packages/core/core/domain/policy.py) maps each fault to
its code, and [`domain/transport.py`](../packages/core/core/domain/transport.py)
maps an HTTP status to a fault.

For OSIPTEL, verify the actual exit country before changing retry settings. See
[the OSIPTEL note](osiptel.md) and
[proxy configuration](proxies.md#country-and-site-selection).

## Inspect portal health

Portal breaker state is in PostgreSQL and is keyed by source and provider.
`open_until` is null or in the past when the pair may be claimed:

```sql
select source, provider, consecutive_failures, level, open_until, updated_at
from portal_circuit_breakers
order by source, provider;
```

Portal workers also report heartbeats and held proxy slots:

```sql
select worker_id, tailscale_hostname, last_seen_at, revoked_at
from portal_workers
order by worker_id;

select provider, worker_id, lane_index, slot_id, lease_expires_at
from portal_proxy_slots
where worker_id is not null
order by provider, worker_id, slot_id;
```

To inspect retry cost and latency, query the per-attempt ledger. It contains
every attempt written by a worker, including an attempt from a publish whose
lease fence was already stale. `portal_job_items` and `portal_entries` retain
only the terminal outcome.

```sql
select a.job_item_id,
       a.source,
       a.provider,
       a.worker_id,
       a.lane_index,
       a.fetch_attempt,
       a.outcome,
       a.error_code,
       a.elapsed_ms,
       a.created_at
from portal_lookup_attempts a
order by a.created_at desc
limit 100;
```

Join `portal_lookup_attempts.job_item_id` to `portal_job_items.id` and then to
`portal_jobs` when investigating one job. Do not interpret a failed attempt as
the final item state without checking the terminal row.

These queries only read. Run them against the portal database with
`psql "$PORTAL_DATABASE_DSN"`. To stop a job, cancel it from its page instead of
editing rows.

## Session rotation

Inspect `session_id` and `proxy_id` in the outcome row or exported error row.
The fetch session budget is site-specific and can be lowered with
`--session-budget`. Browser rejection retries and session restarts are
independent settings. Read the relevant package `readme.md` before changing
either.

If restarting a session does not change the rejection class, check the provider
and site gate before increasing retry counts. Retries cannot repair invalid
credentials, a blocked exit, or a browser-only protocol.
