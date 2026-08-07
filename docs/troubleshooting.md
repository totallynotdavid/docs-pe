# Diagnosing job failures

This is a runbook, organized by symptom. It assumes you're looking at a real job
that seems stuck or failing; it doesn't explain the mechanisms (circuit breaker,
retry, sticky sessions): that's [architecture.md](architecture.md) and
[proxies.md](proxies.md).

## Check the state database, not logs

Logs count attempts, not documents. A rerun retrying 30 documents across 4
attempts and 25 lanes produces 3,000 log lines from only 120 document attempts.

Always query the state database instead:

```bash
python scripts/check-job-status.py
```

This shows the real distribution of outcomes (success, not_found, error,
rejected, pending).

## Circuit breaker is holding (lanes parked for one provider)

When you rerun a job, known failures are retried first. If 10 consecutive
deterministic failures occur before a success, the circuit breaker trips for
that `(site, provider)` pair. All lanes for that pair park until the first
success, then recover.

Example: relaunch with 30 known bad documents on 25 lanes. DataImpulse lanes all
fail on the first 10 documents (deterministic), so the breaker trips. GeoNode
lanes continue. After the first GeoNode success, the DataImpulse breaker
recovers. One box without accumulated failures continued at full speed; another
box paused for ~5 minutes while the first document retried.

**This is expected and normal.** It's a failsafe to prevent cascading failures.
Check for success in the state database and wait for recovery.

To confirm, query the database for the first success after the breaker tripped:

```sql
select doc, status, error_code, timestamp
from outcomes
where site = 'osiptel'
order by timestamp desc
limit 20;
```

You should see a series of errors, then a success. The success unblocks the
breaker.

## Distinguishing provider failure from document failure

A deterministic document failure can trip the circuit breaker and look like a
provider outage.

**Provider failure signs:**

- `407` across every lane (account out of traffic or suspended)
- `TRAFFIC_EXHAUSTED` error (DataImpulse explicit)
- Failures resume after breaker recovery (provider is truly down)

**Circuit breaker false alarm:**

- Failures pause and then resume after first success
- High error count from only a few documents (deterministic, not random)
- All the same `error_code` (e.g., all `ban_signal`): likely a deterministic
  document issue, not a provider issue

Confirm from a second box before changing concurrency: all boxes share the same
provider accounts.

## Read an active run

During execution, the CSV doesn't exist yet. Query the database for progress:

```bash
python scripts/check-job-status.py
```

Repeat every few minutes to track progress.

Expected progression: early on, many `pending` (work not yet claimed); mid-run,
many `success` with some `error`/`rejected`; late, mostly `success` and final
outcomes.

If `pending` isn't decreasing: check the worker process is running, then break
down by provider:

```bash
python scripts/check-provider-breakdown.py
```

If one provider is stuck, check the circuit breaker (above).

## 407 or TRAFFIC_EXHAUSTED

Both mean the provider account is out of traffic or suspended.

- **407**: HTTP response code: proxy rejected the request (auth failed or no
  traffic)
- **TRAFFIC_EXHAUSTED**: DataImpulse's explicit error message

Confirm from a second box before concluding the account is really exhausted. It
might be a temporary rate limit.

## High error rate on OSIPTEL

Check `GEONODE_COUNTRY=PE` and `DATAIMPULSE_COUNTRY=pe` are both set (see
[proxies.md](proxies.md#peru-exits-are-mandatory-for-osiptel)). If they're set
and DataImpulse is still failing heavily, that's expected: see
[sites/osiptel.md](sites/osiptel.md) for measured failure rates and why
GeoNode-only is the standard configuration.

## Attribute failures only after a large sample

Early in a run, a 20-failure sample might split close to evenly between
providers: too small to distinguish signal from noise. At four-digit scale,
OSIPTEL's GeoNode-to-DataImpulse failure ratio is roughly 1000:1. Query only
after at least 1000 document attempts per provider.

## All lanes idle, but job still running

Check the circuit breaker: one provider might be parked after 10 consecutive
failures while another continues:

```bash
python scripts/check-circuit-breaker.py
```

If one provider shows no recent outcomes, it's parked. Wait for recovery.

## Session budget exhausted

If a lane rotates faster than expected, it might be exhausting its session
budget too quickly. OSIPTEL requires `--session-budget=1` (one lookup per
session); SUNAT typically uses 50.

```sql
select session_id, count(*) as lookups
from outcomes
where doc = '12345678'
group by session_id;
```

More `session_id` values per document means faster rotation.

## Session restart didn't help (browser)

`browser` restarts the session with a fresh proxy exit after
`--reject-restart-threshold` consecutive exhausted subjects. If the account
itself is out of traffic (see 407/TRAFFIC_EXHAUSTED above), a restart just gets
another exit from the same exhausted account and doesn't help. Check the
account, not the session.

## Do not diagnose from log volume

A relaunch produces a large error burst from only a small fraction of input:

```
30 documents × 4 attempts × 25 lanes = 3,000 log lines
```

That's 120 actual document attempts, not 3,000 failures. Use the state database.

## Port collision (GeoNode)

GeoNode allocates 901 sticky-port slots. With more than 900 concurrent lanes
across all sites, ports collide and sessions interfere.

Symptom: errors increase with lane count, then plateau or degrade.

Fix: reduce concurrency or split runs across boxes.

## See also

- [architecture.md](architecture.md): circuit breaker and retry semantics
- [proxies.md](proxies.md): provider tuning
- [sites/](sites/): per-site failure modes and error codes
