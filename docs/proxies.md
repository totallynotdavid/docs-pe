# Proxy configuration

`fetch` and the portal use the core provider schema. `browser` uses the same
provider names and credentials, but its loader supports only the browser
settings described in [the browser package](../packages/browser/browser/proxy.py).
`capture` uses the operator's own Chrome profile.

## Configure providers

Select one or more providers with `PROXY_PROVIDER`:

```env
PROXY_PROVIDER=geonode:30,dataimpulse:18
```

The syntax is a comma-separated list of `name[:lanes]`. An omitted lane count
uses the provider default. A provider may appear only once. The provider
registry and each provider's `ProviderSpec` are authoritative for names,
defaults, choices, and validation:
[`core/proxy/geonode.py`](../packages/core/core/proxy/geonode.py) and
[`core/proxy/dataimpulse.py`](../packages/core/core/proxy/dataimpulse.py).

GeoNode fields:

```env
GEONODE_USERNAME=<username>
GEONODE_PASSWORD=<password>
GEONODE_GATEWAY=fr
GEONODE_PROXY_TYPE=residential
GEONODE_COUNTRY=PE
GEONODE_STATE=
GEONODE_CITY=
GEONODE_ASN=
GEONODE_STRICT_OFF=
GEONODE_LIFETIME_MINUTES=10
```

`GEONODE_GATEWAY` accepts `fr`, `fr_whitelist`, `us`, or `sg`.
`GEONODE_PROXY_TYPE` accepts `residential`, `datacenter`, or `mix`.
`GEONODE_COUNTRY` accepts a two-letter code in either case and is normalized to
uppercase. For `fetch` and the portal, `GEONODE_STATE`, `GEONODE_CITY`,
`GEONODE_ASN`, and `GEONODE_STRICT_OFF` are optional advanced filters. Browser
ignores those fields.
`GEONODE_LIFETIME_MINUTES` must be between 3 and 1,440; it controls the
provider session lifetime.

DataImpulse fields:

```env
DATAIMPULSE_USERNAME=<username>
DATAIMPULSE_PASSWORD=<password>
DATAIMPULSE_COUNTRY=pe
DATAIMPULSE_SESSION_MINUTES=3
```

`DATAIMPULSE_COUNTRY` accepts a two-letter code in either case and is normalized
to lowercase. For `fetch` and the portal, `DATAIMPULSE_SESSION_MINUTES` must be
between 1 and 1,440. Browser accepts any positive value. These bounds are
enforced by the respective loaders.

Each provider module owns its field schema. `core.proxy.base` supplies the
shared field and provider-spec types used by environment loading, stored portal
credentials, and the portal form. When adding a provider, add its schema and
registry entry before adding provider-specific documentation.

## Country and site selection

OSIPTEL requires a Peru exit. Set the country explicitly for every provider that
may receive OSIPTEL work:

```env
GEONODE_COUNTRY=PE
DATAIMPULSE_COUNTRY=pe
```

The [OSIPTEL note](sites/osiptel.md) describes the site's blocked-exit signal.

## Sticky slots

GeoNode maps sticky sessions to a finite port range. A standalone fetch process
assigns unique slots only inside that process. `fetch-fleet` rejects a
multi-shard GeoNode job until it has a shared slot allocator. Run one standalone
GeoNode job at a time.

Portal workers use the `portal_proxy_slots` table to coordinate GeoNode slots
across the fleet. A lane claims a slot while it works a provider credential,
renews the lease through the worker heartbeat, and releases it when it changes
provider, becomes idle, or shuts down. An unrenewed lease expires, so a crashed
worker does not reserve a slot forever.

This coordination applies to portal workers, not to standalone fetch processes.
Inspect current leases in [Portal operations](../packages/portal/operations.md).

## Preflight

The provider registry exposes a preflight helper that opens a real session and
returns the observed exit IP. Run it with the same environment used by the job:

```sh
uv run --env-file .env fetch-preflight --provider geonode
```

Use the provider name that will run the job. The command prints the provider
name and observed exit IP, without printing credentials. A successful preflight
verifies provider connectivity for one session, not acceptance by the target
site. Run a small target-site job before a large one.

## Diagnose provider failures

Use the [troubleshooting runbook](operations/troubleshooting.md) to distinguish
provider transport failures, site blocks, and document-level results. The
standalone outcome database stores breaker state by `(site, provider)`. The
portal stores fleet breaker state by `(source, provider)` in PostgreSQL.
