# Results ledger

Historical, empirical data from completed jobs. Use it to calibrate expectations
for new runs and to sanity-check a job in progress. This is a log, not a
reference doc: add new rows as jobs complete, don't fold new mechanism
explanations in here. If you're explaining _why_ something behaves a certain
way, that belongs in [architecture.md](architecture.md),
[proxies.md](proxies.md), or [sites/](sites/); link to it instead of re-deriving
it here.

All output files live in `results/<job>/`, which is gitignored. The state
database `*.state.sqlite3` is the source of truth; CSVs are disposable
projections.

## Reconciliation rules

OSIPTEL allows empty results (see [sites/osiptel.md](sites/osiptel.md)):

```
documents with >= 1 line + documents with 0 lines + terminal failures = input rows
```

SUNAT and SUNAT reps require output for every accepted document (see
[sites/sunat.md](sites/sunat.md)):

```
result rows + error rows + not_found rows = input rows
```

## OSIPTEL

Phone lines per document. Input is DNIs unless noted.

| Job            |   Input | With lines | Zero-line |    Rows | Failed |
| -------------- | ------: | ---------: | --------: | ------: | -----: |
| barranca_dni   |  50,455 |     41,458 |     8,997 | 107,536 |      0 |
| ancash_dni     |  69,487 |     66,371 |     3,116 | 174,833 |      0 |
| la_libertad    |  71,234 |     66,521 |     4,713 | 170,747 |      0 |
| dni_lambayeque | 208,336 |    195,501 |    12,835 | 518,117 |      0 |
| surco_dni      | 235,002 |    166,967 |    68,035 | 323,981 |      0 |

Zero-line share is a useful sanity check for new DNI jobs. Across these 634,514
documents, it's 15.4%, ranging from 4.5% (Ancash) to 29.0% (Surco). Jobs outside
roughly 4-30% deserve investigation; results near 0% or 100% usually indicate a
bug, not real data.

Carrier distribution across 1,295,214 lines:

| Carrier                   |   Lines | Share |
| ------------------------- | ------: | ----: |
| America Movil (Claro)     | 394,009 | 30.4% |
| Entel                     | 372,781 | 28.8% |
| Telefonica (Movistar)     | 337,068 | 26.0% |
| Viettel (Bitel)           | 190,964 | 14.7% |
| Guinea Mobile (Cuy Movil) |     268 | 0.02% |
| Flash Servicios           |     124 | 0.01% |

## SUNAT

Identity records for RUC-10 (natural persons).

| Job        |   Input |    Rows | Errors | Not found |
| ---------- | ------: | ------: | -----: | --------: |
| barranca   |  50,490 |  50,455 |     35 |         0 |
| ancash     | 112,478 | 112,437 |     41 |         0 |
| surco      | 235,236 | 235,233 |      0 |         3 |
| lambayeque | 394,884 | 394,729 |    155 |         0 |
| trujillo   | 540,756 | 540,606 |    150 |         0 |

Jobs before 2026-08-01 had a residual error rate of 0.03-0.04%, entirely
explained by two parser gaps (sucesión indivisa handling and non-DNI `tipo_doc`
values, see [sites/sunat.md](sites/sunat.md)) fixed after that date. `surco` was
the first rerun after those fixes and reached zero errors.

## SUNAT reps

Legal representatives for RUC-20 (entities).

| Job        |   Input |    Rows | Errors |
| ---------- | ------: | ------: | -----: |
| ruc20_reps | 846,047 | 931,419 |      9 |

More rows than input is expected: entities have multiple representatives, some
have none.

## Throughput

Proven lane configurations per box (two boxes typical):

| Site    | Configuration               | Throughput                           |
| ------- | --------------------------- | ------------------------------------ |
| OSIPTEL | `geonode:30`                | 27,220 documents/hour, zero failures |
| SUNAT   | `geonode:20,dataimpulse:20` | 74 documents/s                       |
| SUNAT   | `geonode:25`                | 30.6 documents/s                     |

GeoNode showed no meaningful per-lane degradation between 15 and 60 lanes (9.5
s/lookup at 15 lanes, 7.9 s/lookup at 60 lanes): its 901-slot sticky-port
allocation was not the limiting factor at this scale. DataImpulse stayed stable
at 20 lanes even with every lane sharing `gw.dataimpulse.com:823`: a
600-document OSIPTEL probe completed 600 of 600 with `attempt_count=0`.

These are empirical measurements from 2026-08-01 runs; expect drift as provider
infrastructure changes.

## Cost per lookup

Measured against the origin, 2026-08-01.

Proxy dashboards overestimate: both providers bill one request per proxied
session, not per lookup. With `session_budget > 1`, one session serves many
lookups.

| Site    | Per lookup   | Notes                                                             |
| ------- | ------------ | ----------------------------------------------------------------- |
| OSIPTEL | ~16 KB, ~6 s | `session_budget=1` (1:1 sessions to lookups); dashboard is honest |
| SUNAT   | ~26 KB       | `session_budget=50`; the ~23 KB home GET is paid once per session |

A 235,000-document SUNAT job transfers ~6 GB; the dashboard reported 937-982 KB
per billed request (~36 lookups' worth of traffic per billed request). In
practice, throughput is limited by latency and lane count, not bandwidth.
