# OSIPTEL

`checatuslineas.osiptel.gob.pe`. Implemented in
[fetch](../../packages/fetch/readme.md) (plain HTTP, no gate beyond the WAF).
Accepts any document; returns one row per registered phone line (`modalidad`,
redacted `numero`, `operador`), via a paginated POST using DataTables-style
parameters, with `IdTipoDoc` set to 1 for DNI and 2 for RUC. Each page returns
up to 5,000 rows.

## WAF and Peru-exit requirement

OSIPTEL geo-gates by exit IP. Set both explicitly in every `.env`:

```env
GEONODE_COUNTRY=PE
DATAIMPULSE_COUNTRY=pe
```

A non-Peru exit gets a `status=500` block page during the home-page warmup,
classified as `BanSignalError`. An empty `GEONODE_COUNTRY` is especially
dangerous: GeoNode then falls back to its global residential pool, and OSIPTEL
blocks 85-95% of those exits: identical code produced roughly 10% success with
the global pool versus 98.6% with Peru exits.

Suspicious exits can also receive HTTP 200 with a CAPTCHA wall instead of a
clean block, so the readiness check inspects the response body for a success
marker rather than trusting the status code alone.

Example Peru exit prefixes seen in passing traffic: `38.25.x`, `64.76.x`,
`179.6.x`, `181.67.x`, `200.215.x`, `201.218.x`. Use
[the preflight check](../proxies.md#providers) to confirm your current exit
rather than relying on this list, since providers rotate pools.

## Provider failure modes

Measured across 122,000 documents: GeoNode failed 0.03%, DataImpulse failed
20-32%, and every `ban_signal` came from DataImpulse. Two failure modes
dominated:

- `upstream_not_ready`: a `ConnectError` with `status=0`, caused by dead exits
  in the DataImpulse pool. This is _not_ a `407` and does _not_ indicate
  exhausted account traffic. See
  [troubleshooting.md](../troubleshooting.md#distinguishing-provider-failure-from-document-failure)
  for that distinction.
- `ban_signal`: OSIPTEL returned `status=500` because DataImpulse supplied a
  non-Peru exit despite `DATAIMPULSE_COUNTRY=pe` being set. The country field is
  a request hint, not a guarantee.

DataImpulse held 40% of the lanes in one run but produced only 5% of the rows:
including it adds little throughput and requires a recovery pass for the
documents it fails. On 2026-08-02, a 235,002-document run had 6,825 failures
(6,800 DataImpulse, 25 GeoNode). Rerunning those 6,825 documents with GeoNode
only, same site, same hour, completed all of them with zero failures in about 23
minutes. This is why [proxies.md](../proxies.md#provider-selection-by-site)
recommends GeoNode only for OSIPTEL.

Sample size matters: an earlier 20-failure sample split 9 DataImpulse to 11
GeoNode and suggested the opposite conclusion. At four-digit scale the real
ratio is roughly 1000:1. Don't attribute a provider failure from a sample under
~1000 attempts (see
[troubleshooting.md](../troubleshooting.md#distinguishing-provider-failure-from-document-failure)).

## Empty results

A document with no registered phone lines is a valid, non-error result. In
practice empty-result share ranges from about 4% to 30% of a DNI job depending
on province; see [results.md](../results.md) for measured job distributions and
the reconciliation formula that accounts for empty results. A job outside that
range deserves investigation, not a retry.
