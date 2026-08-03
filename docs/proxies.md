# Proxy behaviour, measured

What the two vendors actually do under load, per site. The rules here were
expensive to learn and none of them are inferable from a vendor's documentation.

Configuration syntax and defaults are in
[the fetch manual](../packages/fetch/readme.md#configure). This file is the
evidence.

## Provider choice is a property of the site

| Site         | Provider        | Why                                          |
| ------------ | --------------- | -------------------------------------------- |
| `osiptel`    | `geonode` alone | DataImpulse exits fail 20% to 32% of lookups |
| `sunat`      | both            | DataImpulse is usually the faster half       |
| `sunat_reps` | both            | Same as `sunat`                              |

The account is not the variable. The same DataImpulse account that cannot serve
OSIPTEL carried the larger half of the `surco` SUNAT run: OSIPTEL geo-gates and
SUNAT does not.

### The OSIPTEL finding

Measured over 122,000 documents mid-`surco`: `geonode` failed 0.03%, DataImpulse
failed 20% to 32%, and every `ban_signal` in the run was DataImpulse. Two
failure modes, neither retryable in practice:

- `upstream_not_ready`, a `ConnectError` with `status=0`. Dead exits in
  DataImpulse's pool. Not a `407`, so not an out-of-credits problem.
- `ban_signal`, OSIPTEL returning `status=500`. That is the WAF rejecting a
  non-Peru exit even though `DATAIMPULSE_COUNTRY=pe` is set. DataImpulse does
  not honour its own country pin on this route.

DataImpulse held 40% of the lanes and produced 5% of the rows, so including it
buys roughly 5% throughput and costs a recovery pass over everything it burned.

Confirmed by controlled re-run on 2026-08-02. `surco_dni` finished 235,002 of
235,002 with 6,825 failures, of which 6,800 were DataImpulse and 25 were
`geonode`. Re-running exactly those 6,825 documents `geonode`-only, same site
and same hour, returned 6,825 of 6,825 with zero failures in about 23 minutes.
Only the provider changed. Burned documents are fully recoverable, so an
accidental DataImpulse run costs a cleanup pass.

### Wait for four digits of failures before attributing one

An earlier reading of the same run concluded the opposite, from a 20-failure
warm-up sample that happened to split 9 DataImpulse to 11 `geonode`. Twenty
failures cannot distinguish a provider fault from a shared one. At four digits
this one lands 1000 to 1.

Provider is attributable per row, because `proxy_id` is `proxy-1-port-NNNNN` for
`geonode` and `dataimpulse-slot-N` for DataImpulse:

```sql
select case when proxy_id like 'dataimpulse%' then 'dataimpulse' else 'geonode' end
```

## Peru exits are mandatory for OSIPTEL

`GEONODE_COUNTRY=PE` and `DATAIMPULSE_COUNTRY=pe` must be set explicitly in
every `.env`. A non-Peru exit gets a `status=500` block page at the home-page
GET, which the code classifies as `BanSignalError`.

An _empty_ country is the trap, more than a wrong one: GeoNode then serves a
global residential pool, OSIPTEL blocks 85% to 95% of those exits, and only
random Peru draws succeed. This produced a run at roughly 10% success against
98.6% in production on identical code, and cost a day of chasing TLS
fingerprints and warm-up theories before the exit pool turned out to be the
whole story.

Peru exits look like `38.25.x`, `179.6.x`, `181.67.x`, `200.215.x`, `201.218.x`,
or `64.76.x`. `fetch.proxy.registry.preflight` dials a real session through the
provider and returns the exit IP, which is the way to check an `.env` without
waiting for a run to fail.

## Lane counts

Lanes spawn per provider, so `geonode:30,dataimpulse:20` is 50 lanes, not 30.

| Site      | Proven setting                                 | Throughput                           |
| --------- | ---------------------------------------------- | ------------------------------------ |
| `osiptel` | `geonode:30` per box, two boxes                | 27,220 documents/hour, zero failures |
| `sunat`   | `geonode:20,dataimpulse:20` per box, two boxes | 74 documents/s                       |
| `sunat`   | `geonode:25` per box, two boxes                | 30.6 documents/s                     |

`geonode` shows no per-lane degradation from 15 to 60 lanes: 7.9 s per lookup at
60 against 9.5 s at 15. Its sticky ports allow 901 slots, so ports are never the
limit. None of these are ceilings. Raise them while watching `error_code` in the
state database, not the log.

DataImpulse does not collapse at 20 lanes either, despite every lane sharing
`gw.dataimpulse.com:823`. The shared port is real but imposes no concurrency
limit: a 600-document OSIPTEL probe at 20 lanes returned 600 of 600 with
`attempt_count=0` on every row. The failures once blamed on lane contention were
the account being out of traffic, which its usage export reports as
`TRAFFIC_EXHAUSTED`. Check the dashboard for traffic before blaming concurrency.

## Reading a run in progress

A `407` on every lane means the account is out of credits or suspended. It is
never a per-server problem: all boxes share one GeoNode account and one
DataImpulse account, so confirm from a second box.

A deterministic per-document fault can park a healthy provider and look exactly
like an outage. `pipeline/fetch.py` records every fault against the breaker,
because every fault is environmental, so ten consecutive deterministic failures
trip that provider's circuit breaker. A relaunch retries known-bad documents
first, which delivers them as precisely that consecutive burst. On the `surco`
relaunch this parked all 20 DataImpulse lanes on one box for about five minutes
while a box with no accumulated bad documents ran at full speed. It self-heals
on the first good document. If one provider's row count goes flat while its
sibling keeps moving, check for this before suspecting the account.

For the same reason, never triage from the log. A relaunch opens with every lane
emitting `lookup_failed` on the same handful of known-bad documents at attempt
2, 3, and 4, with the breaker escalating. It reads like a total collapse. Log
lines count _attempts_, so 30 bad documents times 4 attempts times 25 lanes is a
wall of text generated by 0.1% of the input. Read
`select status, count(*) from outcomes` instead. This cost two healthy runs,
killed on suspicion.
