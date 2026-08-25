# Proxy configuration

`fetch` and `browser` use proxy providers to distribute lookups across
geographic exits. `capture` doesn't use proxies; it uses your own Chrome. This
doc covers provider mechanics and tuning: stable reference, not measurements.
For why a given site needs a given provider, see [sites/](sites/); for
throughput and cost numbers from real jobs, see
[the results ledger](reports/results.md).

## Providers

GeoNode is a residential proxy, reliable across sites, with per-lane port
allocation.

```env
GEONODE_USERNAME=<username>
GEONODE_PASSWORD=<password>
GEONODE_GATEWAY=fr              # fr | fr_whitelist | us | sg
GEONODE_PROXY_TYPE=residential  # residential | datacenter | mix
GEONODE_COUNTRY=PE              # uppercase, required for OSIPTEL
GEONODE_LIFETIME_MINUTES=10     # 3..1440
```

Gateway selection depends on your location and the target site. `fr_whitelist`
is a curated subset. Lifetime is session expiration; shorter lifetime rotates
exits more aggressively. GeoNode uses one port per lane slot, starting at 10000,
allocated globally across all sites; a random `sessionId` in the username string
rotates the exit, and sessions are explicitly released via API (example proxy
ID: `proxy-1-port-10023`). It allocates 901 sticky-port slots total, so with
more than 900 concurrent lanes across all sites, ports collide and sessions
interfere: reduce concurrency or split runs across boxes.

DataImpulse is a rotating datacenter proxy, cheaper per request, but fails often
against OSIPTEL's geo-gated WAF (see
[sites/osiptel.md](sites/osiptel.md#provider-failure-modes) for the measured
rate).

```env
DATAIMPULSE_USERNAME=<username>
DATAIMPULSE_PASSWORD=<password>
DATAIMPULSE_COUNTRY=pe          # lowercase ISO-3166, required for OSIPTEL
DATAIMPULSE_SESSION_MINUTES=3   # >= 1
```

DataImpulse uses one rotating port (`gw.dataimpulse.com:823`); stickiness is
stored in the `sessid` field of the username, and sessions expire by TTL
(configurable), no explicit release needed (example proxy ID:
`dataimpulse-slot-5`). `proxy_id` tells you which provider produced a row
without joining anything:

```sql
select
  case when proxy_id like 'dataimpulse%' then 'dataimpulse' else 'geonode' end,
  status,
  count(*)
from outcomes
group by 1, 2;
```

Provider fields follow `<PROVIDER>_<FIELD>` and are defined by each provider's
`Field` schema in `fetch/proxy/base.py`. The same schema validates environment
variables, stored credentials, and the portal form. Adding a provider requires
one module and one entry in `fetch/proxy/registry.py`.

Lanes themselves are configured globally, across both providers, in
`PROXY_PROVIDER`:

```env
PROXY_PROVIDER=geonode:30,dataimpulse:18
```

This creates 30 GeoNode lanes and 18 DataImpulse lanes (48 total). Lanes are
created per provider, so provider failover happens at the lane level: if all
GeoNode lanes fail, DataImpulse lanes keep working. Omitting `:lanes` uses the
provider default; unknown names or duplicates fail at startup. `browser` uses a
single session, so it takes only the first provider listed and ignores lane
counts.

Before starting a run, verify your proxy configuration is working:

```python
from fetch.proxy.registry import preflight

result = preflight("geonode")
print(f"Exit IP: {result}")
```

This opens a real provider session and returns the exit IP. Use it to confirm
the country setting is correct before running a large job.

## Peru exits are mandatory for OSIPTEL

```env
GEONODE_COUNTRY=PE
DATAIMPULSE_COUNTRY=pe
```

Both fields default to a Peru code when unset and reject an explicitly empty
value at startup (`fetch/proxy/load.py`). See
[sites/osiptel.md](sites/osiptel.md#waf-and-peru-exit-requirement) for why Peru
exits matter and the measured failure rates.

## Provider selection by site

| Site       | Provider     | Why                                     |
| ---------- | ------------ | --------------------------------------- |
| OSIPTEL    | GeoNode only | DataImpulse fails often (geo-gated WAF) |
| SUNAT      | Both         | Not geo-gated; use both to split load   |
| SUNAT reps | Both         | Same behavior as SUNAT                  |

Provider suitability is a property of the site, not the account: the same
DataImpulse account that fails against OSIPTEL handles half a SUNAT run without
issue. Details and measured failure rates: [sites/osiptel.md](sites/osiptel.md),
[sites/sunat.md](sites/sunat.md). For lane and circuit-breaker mechanics in the
pipeline, see [ARCHITECTURE.md](../ARCHITECTURE.md); for diagnosing 407s,
circuit breaker false alarms, or port exhaustion, see
[troubleshooting.md](operations/troubleshooting.md).
