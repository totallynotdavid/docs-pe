# Results ledger

Historical measurements from completed jobs. Use these numbers to calibrate a
new run, not to define runtime behavior. Normative contracts belong in
[Architecture](../../ARCHITECTURE.md), [proxy configuration](../proxies.md), or
the matching [site note](../sites/).

Output files live in `results/<job>/`, which is gitignored. CSVs are disposable
projections of the recorded outcomes.

## Reconciliation rules

OSIPTEL permits an empty result:

```text
documents with lines + documents without lines + terminal failures = input rows
```

SUNAT requires an identity outcome for each accepted document:

```text
result rows + error rows + not_found rows = input rows
```

SUNAT representatives are different. An entity may have no representative, so a
successful empty result is valid. See [the SUNAT site note](../sites/sunat.md).

## OSIPTEL

Phone lines per document. Input is DNIs unless noted.

| Job                   |   Input | With lines | Zero-line |      Rows | Failed |
| --------------------- | ------: | ---------: | --------: | --------: | -----: |
| barranca_dni          |  50,455 |     41,458 |     8,997 |   107,536 |      0 |
| ancash_dni            |  69,487 |     66,371 |     3,116 |   174,833 |      0 |
| la_libertad           |  71,234 |     66,521 |     4,713 |   170,747 |      0 |
| dni_lambayeque        | 208,336 |    195,501 |    12,835 |   518,117 |      0 |
| surco_dni             | 235,002 |    166,967 |    68,035 |   323,981 |      0 |
| lima_dni              | 241,433 |    186,735 |    54,698 |   396,280 |      0 |
| la_molina_dni         |  99,484 |     69,602 |    29,882 |   133,044 |      0 |
| piura_dni             | 566,239 |    485,025 |    81,214 | 1,248,310 |      0 |
| tumbes_dni            |  45,225 |     38,886 |     6,339 |   120,225 |      0 |
| ica_dni               | 210,145 |    176,164 |    33,981 |   413,877 |      0 |
| ayacucho_dni          |  22,267 |     20,237 |     2,030 |    49,790 |      0 |
| cajamarca_dni         |  23,892 |     20,662 |     3,230 |    52,052 |      0 |
| comas_dni             |  48,514 |     39,393 |     9,121 |    90,464 |      0 |
| sjm_dni               |  36,102 |     29,800 |     6,301 |    69,943 |      1 |
| smp_dni               |  64,177 |     52,414 |    11,763 |   121,778 |      0 |
| los_olivos_dni        |  38,775 |     31,904 |     6,871 |    74,595 |      0 |
| villa_el_salvador_dni |  37,109 |     31,550 |     5,559 |    76,817 |      0 |
| lince_dni             |   9,530 |      7,534 |     1,996 |    16,924 |      0 |
| puente_piedra_dni     |  27,649 |     23,793 |     3,856 |    57,289 |      0 |
| vmt_dni               |  31,238 |     26,133 |     5,105 |    61,773 |      0 |
| jesus_maria_dni       |  11,894 |      9,388 |     2,506 |    20,369 |      0 |
| el_agustino_dni       |  15,791 |     13,000 |     2,791 |    30,230 |      0 |
| independencia_dni     |  19,780 |     16,104 |     3,676 |    37,588 |      0 |
| tacna_dni             |  35,545 |     31,212 |     4,333 |    81,387 |      0 |
| junin_con_negocio_dni |  48,724 |     41,883 |     6,841 |    99,873 |      0 |

Across these 2,268,027 documents, 16.7% returned no lines. The observed range is
4.5% in Ancash to 30.0% in La Molina. Values near 0% or 100% usually point to a
bug.

Carrier distribution across 3,208,834 lines, before the five district jobs above
were folded into the breakdown:

| Carrier                   |   Lines |  Share |
| ------------------------- | ------: | -----: |
| America Movil (Claro)     | 904,824 |  28.2% |
| Entel                     | 937,428 |  29.2% |
| Telefonica (Movistar)     | 840,462 |  26.2% |
| Viettel (Bitel)           | 525,387 |  16.4% |
| Guinea Mobile (Cuy Movil) |     605 | 0.019% |
| Flash Servicios           |     128 | 0.004% |

## SUNAT

Identity records for RUC-10 natural persons.

| Job               |   Input |    Rows | Errors | Not found |
| ----------------- | ------: | ------: | -----: | --------: |
| barranca          |  50,490 |  50,455 |     35 |         0 |
| ancash            | 112,478 | 112,437 |     41 |         0 |
| surco             | 235,236 | 235,233 |      0 |         3 |
| lambayeque        | 394,884 | 394,729 |    155 |         0 |
| trujillo          | 540,756 | 540,606 |    150 |         0 |
| lima              | 241,603 | 241,591 |      0 |        12 |
| la_molina         |  99,568 |  99,567 |      0 |         1 |
| piura             | 566,386 | 566,384 |      0 |         2 |
| tumbes            |  45,255 |  45,255 |      0 |         0 |
| ica               | 210,267 | 210,265 |      0 |         2 |
| ayacucho          |  22,272 |  22,272 |      0 |         0 |
| cajamarca         |  23,903 |  23,903 |      0 |         0 |
| comas             |  48,539 |  48,537 |      0 |         2 |
| sjm               |  36,132 |  36,130 |      1 |         1 |
| smp               |  64,231 |  64,230 |      0 |         1 |
| los_olivos        |  38,805 |  38,803 |      0 |         2 |
| villa_el_salvador |  37,145 |  37,145 |      0 |         0 |
| lince             |   9,533 |   9,533 |      0 |         0 |
| puente_piedra     |  27,660 |  27,660 |      0 |         0 |
| vmt               |  31,266 |  31,266 |      0 |         0 |
| jesus_maria       |  11,900 |  11,900 |      0 |         0 |
| el_agustino       |  15,809 |  15,807 |      0 |         2 |
| independencia     |  19,800 |  19,800 |      0 |         0 |
| tacna             |  35,583 |  35,583 |      0 |         0 |
| junin_con_negocio |  48,768 |  48,767 |      0 |         1 |

Jobs before 2026-08-01 had residual error rates from 0.03% to 0.07%. Two parser
gaps explained them: sucesión indivisa handling and non-DNI `tipo_doc` values.
See [the SUNAT site note](../sites/sunat.md).

## SUNAT representatives

| Job        |   Input |    Rows | Errors |
| ---------- | ------: | ------: | -----: |
| ruc20_reps | 846,047 | 931,419 |      9 |

More rows than input is expected because entities can have multiple
representatives. Some entities have none.

## Throughput

| Site    | Configuration               | Throughput                           |
| ------- | --------------------------- | ------------------------------------ |
| OSIPTEL | `geonode:30`                | 27,220 documents/hour, zero failures |
| SUNAT   | `geonode:20,dataimpulse:20` | 74 documents/s                       |
| SUNAT   | `geonode:25`                | 30.6 documents/s                     |

GeoNode showed no meaningful degradation between 15 and 60 lanes: 9.5 seconds
per lookup at 15 lanes and 7.9 seconds at 60 lanes. DataImpulse stayed stable at
20 lanes in a 600-document OSIPTEL probe.

These measurements came from 2026-08-01 runs and will drift as provider
infrastructure changes.

## Cost per lookup

Measured against the origin on 2026-08-01. Providers bill one request per
proxied session, not per lookup. A session budget above one spreads that cost
across several lookups.

| Site    | Per lookup   | Notes                                               |
| ------- | ------------ | --------------------------------------------------- |
| OSIPTEL | ~16 KB, ~6 s | `session_budget=1`                                  |
| SUNAT   | ~26 KB       | `session_budget=50`; home GET paid once per session |

A 235,000-document SUNAT job transferred about 6 GB. Throughput was limited by
latency and lane count, not bandwidth.
