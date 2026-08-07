# SUNAT

`e-consultaruc.sunat.gob.pe`. Implemented in
[fetch](../../packages/fetch/readme.md). Two sites share this endpoint family:

- **sunat**: RUC-10 (natural persons). Returns `tipo_doc`, `num_doc`, `nombre`,
  `tipo_contribuyente`.
- **sunat_reps**: RUC-20 (entities). Returns one row per legal representative:
  `razon_social`, `doc_type`, `num_doc`, `nombre`, `cargo`, `fecha_desde`.

Not geo-gated: unlike OSIPTEL, both GeoNode and DataImpulse work well here. See
[proxies.md](../proxies.md#provider-selection-by-site).

## Request shape

A single POST to `jcrS00Alias` with `accion=consPorRuc`, the RUC, and a random
52-character token. SUNAT's reCAPTCHA wrapper is client-side only: the server
checks that a token is present and plausibly shaped, not that it's genuine.

## sunat: response shapes

Two shapes need special handling:

- **Sucesión indivisa** (succession without division) has no "Tipo de Documento"
  block. Use `tipo_doc=""` and take `nombre` from the RUC row. These are ~0.1%
  of a RUC-10 job and don't produce a DNI for OSIPTEL follow-up. A missing
  document block for any other contributor type is parser drift, not a sucesión
  indivisa: treat it as a bug, not this case.
- An unknown RUC returns a normal page saying "El número de RUC N consultado no
  es válido". This is terminal `not_found` and never retried.

`tipo_doc` is not always `DNI`. One 235,000-row job had 235,003 DNI, 10 CE, and
1 C.FFPP. Filter OSIPTEL follow-ups by `tipo_doc == "DNI"`, not by checking
`num_doc` length.

## sunat_reps: legal representatives

A separate identity request determines whether to fetch representatives at all:
some entity types (associations, educational centers) have no representatives by
law, and that's a valid empty result, not a failure.

## Reconciliation

Unlike OSIPTEL, SUNAT requires every accepted document to produce output:

```
result rows + error rows + not_found rows = input rows
```

`sunat_reps` produces more rows than input documents (one row per
representative). See [results.md](../results.md) for measured job data,
including the residual error rate before/after the 2026-08-01 parser fixes.

## See also

- [packages/fetch/readme.md](../../packages/fetch/readme.md): CLI and output
  files
- [architecture.md](../architecture.md): reconciliation rules in general
- [results.md](../results.md): measured job data
