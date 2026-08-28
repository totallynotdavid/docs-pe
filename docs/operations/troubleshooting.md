# Troubleshooting

Diagnose a run from the state database, not logs or the output CSV: see
[Architecture](../../ARCHITECTURE.md#outcome-state) for why. For proxy
mechanics, read [Proxy configuration](../proxies.md). For site-specific failure
modes, read the matching file in [docs/sites](../sites/).

## Check progress

```sh
uv run python scripts/check-job-status.py
uv run python scripts/check-provider-breakdown.py
uv run python scripts/check-circuit-breaker.py
```

If the count of new outcomes stops, check the worker process and then inspect
the provider breakdown. If one provider has stopped producing outcomes, its
circuit breaker may be parked. A successful contact for that provider clears the
breaker.

## Separate provider failures from document failures

Treat a provider as unhealthy only after checking more than one box and a large
sample. The same provider account is shared across boxes, and a deterministic
document failure can trip the same breaker as a provider outage.

Signs of an exhausted provider include `407` on every lane and
`TRAFFIC_EXHAUSTED`. Signs of a document-level failure include one error code
repeating for a small set of documents, or failures that stop immediately after
the first successful contact.

For OSIPTEL specifically, check `GEONODE_COUNTRY` and `DATAIMPULSE_COUNTRY`
before blaming the account: see
[Peru exits are mandatory for OSIPTEL](../proxies.md#peru-exits-are-mandatory-for-osiptel).

## Check session rotation

Count sessions for one document when a lane rotates unexpectedly:

```sql
select session_id, count(*) as lookups
from outcomes
where doc = '12345678'
group by session_id;
```

See
[fetch's CLI reference](../../packages/fetch/readme.md#command-line-interface)
for each site's default session budget. In `browser`, a session restarts after
the configured rejection threshold. A restart cannot fix an account that is out
of provider traffic.

## Check GeoNode port pressure

See [Proxy configuration](../proxies.md#providers) for GeoNode's sticky-port
slot limit and how the portal worker fleet coordinates it automatically.

See which slots are currently leased and to whom:

```sql
select worker_id, lane_index, slot_id
from portal_proxy_slots
where provider = 'geonode' and worker_id is not null
order by worker_id, slot_id;
```

Every worker node's lanes should hold distinct `slot_id` values. Worker logs
should show `proxy_id=proxy-1-port-<N>` varying per lane instead of pinned to
one port.
