# Results ledger

Every job that has been reconciled end to end. A job is recorded here once its
counts close, so this doubles as the record of what the collectors have produced
and of what a healthy job looks like.

The output files live in `results/<job>/`, which is gitignored. This ledger is
the part that is kept.

## How a job reconciles

`osiptel` sets `allows_empty=True`: a document with no phone lines is a real
success that contributes no CSV rows and does not appear in `.not_found.csv`.
Unique documents in the main CSV are therefore always fewer than the number that
succeeded, and the identity to check is:

```
documents with >= 1 line  +  documents with 0 lines  +  terminal failures  =  input rows
```

`sunat` and `sunat_reps` set `allows_empty=False`, so every accepted document
either produces rows or lands in `.errors.csv` or `.not_found.csv`:

```
result rows  +  errors  +  not_found  =  input rows
```

`sunat_reps` returns one row per legal representative, so it produces more rows
than input documents.

## osiptel

Phone lines per document. Input is DNIs unless noted.

| Job              |   Input | With lines | Zero-line |    Rows | Failed |
| ---------------- | ------: | ---------: | --------: | ------: | -----: |
| `barranca_dni`   |  50,455 |     41,458 |     8,997 | 107,536 |      0 |
| `ancash_dni`     |  69,487 |     66,371 |     3,116 | 174,833 |      0 |
| `la_libertad`    |  71,234 |     66,521 |     4,713 | 170,747 |      0 |
| `dni_lambayeque` | 208,336 |    195,501 |    12,835 | 518,117 |      0 |
| `surco_dni`      | 235,002 |    166,967 |    68,035 | 323,981 |      0 |

Every one of these closes exactly, with zero terminal failures.

The zero-line share is the number to sanity-check a new DNI job against. Across
these 634,514 documents it is 15.4%, and the spread by province is wide: 4.5% in
Ancash against 29.0% in Surco. A job outside roughly 4% to 30% is worth
investigating before trusting; one at 0% or 100% means something is broken.

Carrier distribution over the 1,295,214 lines those runs returned:

| Carrier                   |   Lines | Share |
| ------------------------- | ------: | ----: |
| America Movil (Claro)     | 394,009 | 30.4% |
| Entel                     | 372,781 | 28.8% |
| Telefonica (Movistar)     | 337,068 | 26.0% |
| Viettel (Bitel)           | 190,964 | 14.7% |
| Guinea Mobile (Cuy Movil) |     268 | 0.02% |
| Flash Servicios           |     124 | 0.01% |

## sunat

Identity records for RUC-10 (natural persons).

| Job          |   Input |    Rows | Errors | Not found |
| ------------ | ------: | ------: | -----: | --------: |
| `barranca`   |  50,490 |  50,455 |     35 |         0 |
| `ancash`     | 112,478 | 112,437 |     41 |         0 |
| `surco`      | 235,236 | 235,233 |      0 |         3 |
| `lambayeque` | 394,884 | 394,729 |    155 |         0 |
| `trujillo`   | 540,756 | 540,606 |    150 |         0 |

The residual error rate on the jobs that predate 2026-08-01 is a flat 0.03% to
0.04%, and it is one class: the two parser gaps described in
[the SUNAT section of the fetch manual](../packages/fetch/readme.md#sunat).
`surco` was re-run on the fixed parser and is the first job to reach zero.

## sunat_reps

Legal representatives for RUC-20 (entities).

| Job          |   Input |    Rows | Errors |
| ------------ | ------: | ------: | -----: |
| `ruc20_reps` | 846,047 | 931,419 |      9 |

More rows than input is expected: an entity may list several representatives,
and some (associations, educational centres) list none.

## What a job costs

Measured against the origin on 2026-08-01. A proxy dashboard will disagree: both
vendors count one billable request per proxied _session_, so any site with
`session_budget > 1` looks far more expensive than it is. SUNAT was reported at
937 to 982 KB per counted request, roughly 36 lookups' worth of bytes.

| Site      | Per lookup   | Notes                                                                                  |
| --------- | ------------ | -------------------------------------------------------------------------------------- |
| `osiptel` | ~16 KB, ~6 s | `session_budget=1`, so sessions and lookups are 1:1 and the dashboard figure is honest |
| `sunat`   | ~26 KB       | `session_budget=50`; the 22.8 KB home GET is paid once per session                     |

A 235,000-document SUNAT job is therefore about 6 GB. Bandwidth has never been
the constraint on these jobs. Latency and lane count are.
