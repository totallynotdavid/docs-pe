# Troubleshooting

Start with `fetch-status` or the portal database for the run you are
investigating. Read [Architecture](../../ARCHITECTURE.md#outcome-state) for the
state model, [Proxy configuration](../proxies.md) for provider behavior, and the
[site notes](../readme.md) for site-specific failures.

## Inspect a standalone run

Pass the same output path used by `fetch`. The command derives the state path as
`<output>.state.sqlite3`:

```sh
uv run fetch-status --output results/out.csv
uv run fetch-status --output results/out.csv --minutes 10
```

The command reports outcomes, the recorded provider, breaker state, and recent
activity with `--minutes`. A failed row may still be retryable.

## Query the state database

Use SQLite against the state path when a script is not enough:

```sh
sqlite3 results/out.state.sqlite3 \
  "select status, count(*) from outcomes group by status;"
```

If the host does not provide the SQLite CLI, use a short script through the
development environment. Do not use CSV row counts as progress.

To inspect one document's attempts:

```sql
select site, doc, status, attempt_count, error_code, proxy_id, finished_at
from outcomes
where doc = '12345678'
order by site;
```

## Distinguish failure classes

Check the error code, site, provider, and attempt sample together:

| Signal                                                       | Likely boundary                                                        |
| ------------------------------------------------------------ | ---------------------------------------------------------------------- |
| `407` or `TRAFFIC_EXHAUSTED` across many documents and lanes | Provider credentials or account capacity.                              |
| `ban_signal` during a site readiness check                   | The site rejected the exit or returned a block page.                   |
| `upstream_not_ready` with connection failures                | Provider endpoint or upstream availability.                            |
| One parser or document error on a small set                  | Document data or site response shape.                                  |
| `unknown_error`                                              | An exception escaped the known fault taxonomy and needs investigation. |

For OSIPTEL, verify the actual exit country before changing retry settings. See
[the OSIPTEL note](../sites/osiptel.md) and
[proxy configuration](../proxies.md#country-and-site-selection).

## Inspect portal health

Portal breaker state is in PostgreSQL and is keyed by source and provider:

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

Run these queries through the [portal SQL runbook](../../packages/portal/operations.md)
with the portal database connection.

## Session rotation

Inspect `session_id` and `proxy_id` in the outcome row or exported error row.
The fetch session budget is site-specific and can be lowered with
`--session-budget`. Browser rejection retries and session restarts are
independent settings. Read the relevant package `readme.md` before changing either.

If restarting a session does not change the rejection class, check the provider
and site gate before increasing retry counts. Retries cannot repair invalid
credentials, a blocked exit, or a browser-only protocol.
