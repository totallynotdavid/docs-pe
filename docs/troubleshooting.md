# Diagnosing job failures

This is a runbook, organized by symptom. It assumes you're looking at a real job
that seems stuck or failing; it doesn't explain the mechanisms (circuit breaker,
retry, sticky sessions): that's [architecture.md](architecture.md) and
[proxies.md](proxies.md). For per-site failure modes and error codes, see
[sites/](sites/).

## Read progress from the state database, not logs

Logs count attempts, not documents: a relaunch of 30 known-bad documents across
4 attempts and 25 lanes produces 3,000 log lines from only 120 real document
attempts, which reads like collapse but isn't. During execution the CSV doesn't
exist yet either. Query the database for progress instead, at any point in a
run:

```bash
python scripts/check-job-status.py
```

Expected progression: early on, many `pending` (work not yet claimed); mid-run,
many `success` with some `error`/`rejected`; late, mostly `success` and final
outcomes. If `pending` isn't decreasing, check the worker process is running,
then break down by provider:

```bash
python scripts/check-provider-breakdown.py
```

If one provider is stuck, see circuit breaker symptoms, below.

## Circuit breaker symptoms

When you rerun a job, known failures are retried first. If 10 consecutive
deterministic failures occur before a success, the circuit breaker trips for
that `(site, provider)` pair, and all lanes for that pair park until the first
success, then recover. This can look like a stall: all lanes idle but the job
still running, one provider parked while another continues.

Example: relaunch with 30 known bad documents on 25 lanes. DataImpulse lanes all
fail on the first 10 documents (deterministic), so the breaker trips. GeoNode
lanes continue. After the first GeoNode success, the DataImpulse breaker
recovers. One box without accumulated failures continued at full speed; another
box paused for about 5 minutes while the first document retried.

This is expected: it's a failsafe against cascading failures, not a bug. To
confirm a provider is parked rather than dead, check for recent outcomes:

```bash
python scripts/check-circuit-breaker.py
```

If one provider shows no recent outcomes, it's parked; wait for recovery.
Alternatively, query the database directly for the first success after the
breaker tripped:

```sql
select doc, status, error_code, timestamp
from outcomes
where site = 'osiptel'
order by timestamp desc
limit 20;
```

You should see a series of errors, then a success; the success unblocks the
breaker.

## Distinguishing provider failure from document failure

A deterministic document failure can trip the circuit breaker and look like a
provider outage. Signs of a real provider failure: `407` across every lane
(account out of traffic or suspended), a `TRAFFIC_EXHAUSTED` error
(DataImpulse's explicit message for the same condition), or failures that resume
only after breaker recovery. Signs of a circuit breaker false alarm: failures
that pause and then resume right after the first success, a high error count
from only a few documents (deterministic, not random), or every failure sharing
the same `error_code` (e.g., all `ban_signal`), which points to a document
problem rather than a provider problem.

Confirm from a second box before changing concurrency or concluding an account
is exhausted: all boxes share the same provider accounts, so a temporary rate
limit on one box can look like exhaustion.

For OSIPTEL specifically: check `GEONODE_COUNTRY=PE` and
`DATAIMPULSE_COUNTRY=pe` are both set (see
[proxies.md](proxies.md#peru-exits-are-mandatory-for-osiptel)). If they're set
and DataImpulse is still failing heavily, that's expected: see
[sites/osiptel.md](sites/osiptel.md) for measured failure rates and why
GeoNode-only is the standard configuration. Attribute failures to a provider
only after a large sample, too: early in a run, a 20-failure sample might split
close to evenly between providers, too small to distinguish signal from noise.
At four-digit scale, OSIPTEL's GeoNode-to-DataImpulse failure ratio is roughly
1000:1, so query only after at least 1000 document attempts per provider.

## Session budget and restarts

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

In `browser`, a session restarts with a fresh proxy exit after
`--reject-restart-threshold` consecutive exhausted subjects. If the account
itself is out of traffic (407 or TRAFFIC_EXHAUSTED, above), a restart just gets
another exit from the same exhausted account and doesn't help. Check the
account, not the session.

## Port collision (GeoNode)

Symptom: errors increase with lane count, then plateau or degrade. GeoNode's 901
sticky-port slots are shared globally across all sites (see
[proxies.md](proxies.md#providers)); past 900 concurrent lanes, ports collide
and sessions interfere. Fix: reduce concurrency or split runs across boxes.
