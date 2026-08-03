# Proxy behavior

Configuration syntax and defaults are documented in
[the fetch manual](../packages/fetch/readme.md#configure).

## Provider selection by site

| Site         | Provider       | Reason                                      |
| ------------ | -------------- | ------------------------------------------- |
| `osiptel`    | `geonode` only | DataImpulse fails 20% to 32% of lookups     |
| `sunat`      | both           | DataImpulse usually handles the faster half |
| `sunat_reps` | both           | Same behavior as `sunat`                    |

Provider suitability depends on the site, not the account. The same DataImpulse
account that failed against OSIPTEL handled the larger share of a SUNAT run.
OSIPTEL geo-gates requests; SUNAT does not.

## OSIPTEL

Across 122,000 documents:

- GeoNode failed 0.03%.
- DataImpulse failed 20% to 32%.
- Every `ban_signal` came from DataImpulse.

Two failure modes dominated:

- `upstream_not_ready`: a `ConnectError` with `status=0`, caused by dead exits
  in the DataImpulse pool. This is not a `407` and does not indicate exhausted
  traffic.
- `ban_signal`: OSIPTEL returned `status=500` when DataImpulse supplied a
  non-Peru exit despite `DATAIMPULSE_COUNTRY=pe`.

DataImpulse held 40% of the lanes but produced only 5% of the rows. Including it
adds little throughput and requires a recovery pass for failed documents.

On 2026-08-02, a run completed 235,002 documents with 6,825 failures:

- 6,800 from DataImpulse
- 25 from GeoNode

Rerunning those 6,825 documents with GeoNode only, against the same site and
during the same hour, completed all of them with zero failures in about 23
minutes.

Running OSIPTEL through DataImpulse adds cleanup work rather than useful
capacity.

### Attribute failures only after a large sample

An earlier 20-failure sample split 9 DataImpulse to 11 GeoNode and suggested the
opposite conclusion. That sample was too small to distinguish a provider fault
from shared noise.

At four-digit scale, the failure ratio was approximately 1000 to 1.

`proxy_id` identifies the provider:

- GeoNode: `proxy-1-port-NNNNN`
- DataImpulse: `dataimpulse-slot-N`

```sql
select
  case
    when proxy_id like 'dataimpulse%' then 'dataimpulse'
    else 'geonode'
  end
```

## OSIPTEL requires Peru exits

Set both values explicitly in every `.env`:

```env
GEONODE_COUNTRY=PE
DATAIMPULSE_COUNTRY=pe
```

A non-Peru exit receives a `status=500` block page during the home-page request,
which the application classifies as `BanSignalError`.

An empty country value is especially dangerous. GeoNode then uses its global
residential pool, and OSIPTEL blocks approximately 85% to 95% of those exits.
Only randomly selected Peru exits succeed.

Identical code produced roughly 10% success with the global pool and 98.6% with
Peru exits.

Example Peru exits:

- `38.25.x`
- `64.76.x`
- `179.6.x`
- `181.67.x`
- `200.215.x`
- `201.218.x`

`fetch.proxy.registry.preflight` opens a real provider session and returns the
exit IP. Use it to verify an `.env` before starting a run.

## Lane counts

Lanes are created per provider, so this configuration creates 50 lanes:

```text
geonode:30,dataimpulse:20
```

| Site      | Proven setting                                 | Throughput                           |
| --------- | ---------------------------------------------- | ------------------------------------ |
| `osiptel` | `geonode:30` per box, two boxes                | 27,220 documents/hour, zero failures |
| `sunat`   | `geonode:20,dataimpulse:20` per box, two boxes | 74 documents/s                       |
| `sunat`   | `geonode:25` per box, two boxes                | 30.6 documents/s                     |

GeoNode showed no per-lane degradation between 15 and 60 lanes:

- 15 lanes: 9.5 seconds per lookup
- 60 lanes: 7.9 seconds per lookup

Its sticky-port allocation supports 901 slots, so available ports were not the
limiting factor. Increase concurrency while monitoring `error_code` in the state
database.

DataImpulse also remained stable at 20 lanes even though all lanes used
`gw.dataimpulse.com:823`. A 600-document OSIPTEL probe completed 600 of 600 with
`attempt_count=0`.

## Reading an active run

A `407` across every lane usually means the provider account is out of traffic
or suspended. DataImpulse reports exhausted traffic as `TRAFFIC_EXHAUSTED`.

All boxes share the same GeoNode and DataImpulse accounts, so confirm the result
from a second box before changing concurrency.

### Circuit breakers can resemble provider outages

A deterministic document failure can temporarily park a healthy provider.

`pipeline/fetch.py` records every failure against the circuit breaker. Ten
consecutive deterministic failures therefore trip the provider breaker.

After a relaunch, known failures are retried first. This can create a burst
large enough to park every lane for several minutes.

During one relaunch, all 20 DataImpulse lanes on one box stopped for about five
minutes while another box without accumulated failures continued at full speed.
The provider recovered after the first successful document.

When one provider's row count stops while another continues, check the circuit
breaker and retry order before assuming the provider account is unavailable.

### Do not diagnose from log volume

Logs count attempts, not documents.

A relaunch may immediately retry the same failed documents at attempts 2, 3, and
4 across many lanes:

```text
30 documents × 4 attempts × 25 lanes
```

This produces a large volume of errors from only a small fraction of the input.

Use the state database:

```sql
select status, count(*)
from outcomes
group by status;
```
