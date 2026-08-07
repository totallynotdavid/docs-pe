# SUNAT

`e-consultaruc.sunat.gob.pe`. Implemented in
[fetch](../../packages/fetch/readme.md). Two sites share this endpoint family:
`sunat` is RUC-10 (natural persons), returning `tipo_doc`, `num_doc`, `nombre`,
`tipo_contribuyente`; `sunat_reps` is RUC-20 (entities), returning one row per
legal representative (`razon_social`, `doc_type`, `num_doc`, `nombre`, `cargo`,
`fecha_desde`). Neither is geo-gated: unlike OSIPTEL, both GeoNode and
DataImpulse work well here, see
[proxies.md](../proxies.md#provider-selection-by-site).

The request itself is a single POST to `jcrS00Alias` with `accion=consPorRuc`,
the RUC, and a random 52-character token. SUNAT's reCAPTCHA wrapper is
client-side only: the server checks that a token is present and plausibly
shaped, not that it's genuine.

Two response shapes need special handling in `sunat`. Sucesión indivisa
(succession without division) has no "Tipo de Documento" block: use
`tipo_doc=""` and take `nombre` from the RUC row. These are ~0.1% of a RUC-10
job and don't produce a DNI for OSIPTEL follow-up; a missing document block for
any other contributor type is parser drift, not a sucesión indivisa, and should
be treated as a bug. An unknown RUC returns a normal page saying "El número de
RUC N consultado no es válido", which is terminal `not_found` and never retried.
Also note `tipo_doc` is not always `DNI`: one 235,000-row job had 235,003 DNI,
10 CE, and 1 C.FFPP, so filter OSIPTEL follow-ups by `tipo_doc == "DNI"`, not by
checking `num_doc` length.

For `sunat_reps`, a separate identity request determines whether to fetch
representatives at all: some entity types (associations, educational centers)
have no representatives by law, and that's a valid empty result, not a failure.
It also produces more rows than input documents (one row per representative).

Unlike OSIPTEL, SUNAT requires every accepted document to produce output; see
[results.md](../results.md) for the reconciliation formula, measured job data,
and the residual error rate before/after the 2026-08-01 parser fixes for the two
response shapes above.
