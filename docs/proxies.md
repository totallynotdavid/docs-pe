# Proxy configuration

`fetch` and `browser` use proxy providers to distribute lookups across
geographic exits. `capture` doesn't use proxies; it uses your own Chrome. This
doc covers provider mechanics and tuning: stable reference, not measurements.
For why a given site needs a given provider, see [sites/](sites/); for
throughput and cost numbers from real jobs, see [results.md](results.md).

## Providers

**GeoNode**: residential proxies, reliable across sites, supports per-lane port
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
exits more aggressively.

**DataImpulse**: rotating datacenter proxies, cheaper per request, but 20-32%
failure rate on OSIPTEL (see [sites/osiptel.md](sites/osiptel.md)).

```env
DATAIMPULSE_USERNAME=<username>
DATAIMPULSE_PASSWORD=<password>
DATAIMPULSE_COUNTRY=pe          # lowercase ISO-3166, required for OSIPTEL
DATAIMPULSE_SESSION_MINUTES=3   # >= 1
```

Provider fields follow `<PROVIDER>_<FIELD>` and are defined by each provider's
`Field` schema in `fetch/proxy/base.py`. The same schema validates environment
variables, stored credentials, and the portal form. Adding a provider requires
one module and one entry in `fetch/proxy/registry.py`.

## Lane allocation

Lanes are configured globally in `PROXY_PROVIDER`:

```env
PROXY_PROVIDER=geonode:30,dataimpulse:18
```

This creates 30 GeoNode lanes and 18 DataImpulse lanes (48 total). Lanes are
created per provider, so provider failover happens at the lane level: if all
GeoNode lanes fail, DataImpulse lanes keep working.

Omitting `:lanes` uses the provider default. Unknown names or duplicates fail at
startup.

`browser` uses a single session, so it takes only the first provider listed and
ignores lane counts.

## Sticky sessions

A sticky session is one authenticated proxy connection that stays open across
multiple requests. Rotating it means closing the connection and opening a new
one, sometimes to a different exit IP.

**GeoNode:**

- One port per lane slot, starting at 10000, allocated globally across all sites
- A random `sessionId` in the username string rotates the exit
- Sessions are explicitly released via API
- Example proxy ID: `proxy-1-port-10023`
- Allocates 901 sticky-port slots total. With more than 900 concurrent lanes
  across all sites, ports collide and sessions interfere. Reduce concurrency or
  split runs across boxes.

**DataImpulse:**

- One rotating port (`gw.dataimpulse.com:823`)
- Stickiness is stored in the `sessid` field of the username
- Sessions expire by TTL (configurable); no explicit release needed
- Example proxy ID: `dataimpulse-slot-5`

`proxy_id` tells you which provider produced a row without joining anything:

```sql
select
  case when proxy_id like 'dataimpulse%' then 'dataimpulse' else 'geonode' end,
  status,
  count(*)
from outcomes
group by 1, 2;
```

## Peru exits are mandatory for OSIPTEL

Set both explicitly in every `.env`:

```env
GEONODE_COUNTRY=PE
DATAIMPULSE_COUNTRY=pe
```

OSIPTEL's WAF blocks non-Peru exits. An empty `GEONODE_COUNTRY` is especially
dangerous: GeoNode silently falls back to its global residential pool, and
OSIPTEL blocks 85-95% of those exits. See [sites/osiptel.md](sites/osiptel.md)
for the failure-mode breakdown and measured success rates.

## Provider selection by site

| Site       | Provider     | Why                                      |
| ---------- | ------------ | ---------------------------------------- |
| OSIPTEL    | GeoNode only | DataImpulse fails 20-32% (geo-gated WAF) |
| SUNAT      | Both         | Not geo-gated; use both to split load    |
| SUNAT reps | Both         | Same behavior as SUNAT                   |

Provider suitability is a property of the site, not the account: the same
DataImpulse account that fails against OSIPTEL handles half a SUNAT run without
issue. Details and measured failure rates: [sites/osiptel.md](sites/osiptel.md),
[sites/sunat.md](sites/sunat.md).

## Preflight check

Before starting a run, verify your proxy configuration is working:

```python
from fetch.proxy.registry import preflight

result = preflight("geonode")
print(f"Exit IP: {result}")
```

This opens a real provider session and returns the exit IP. Use it to confirm
the country setting is correct before running a large job.

## See also

- [architecture.md](architecture.md): lane/circuit-breaker mechanics in the
  pipeline
- [troubleshooting.md](troubleshooting.md): 407s, circuit breaker false alarms,
  port exhaustion
- [results.md](results.md): throughput and cost per lookup by configuration
