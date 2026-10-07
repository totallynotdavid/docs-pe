# SUNAT

The SUNAT lookup is available at `https://e-consultaruc.sunat.gob.pe/` and is
implemented in [fetch](../packages/cli/readme.md). The `sunat` and `sunat_reps`
sites use the same endpoint family but accept different RUC kinds: `sunat` takes
a natural person's RUC (not starting with 20) and `sunat_reps` takes a legal
entity's RUC (starting with 20).

## Sites and output

Each output starts with a `doc` column.

| Site         | Input              | Output columns                                                                                       |
| ------------ | ------------------ | ---------------------------------------------------------------------------------------------------- |
| `sunat`      | Natural-person RUC | `tipo_doc`, `num_doc`, `nombre`, `tipo_contribuyente`.                                               |
| `sunat_reps` | Legal-entity RUC   | `razon_social`, `doc_type`, `num_doc`, `nombre`, `cargo`, `fecha_desde`, one row per representative. |

The site sends a form-encoded POST to `jcrS00Alias` with `accion=consPorRuc`.
The request includes a random token with the shape expected by the site's
client-side wrapper. The server does not require a browser session or a genuine
reCAPTCHA token.

## `sunat` response rules

An unregistered RUC returns an ordinary result page containing the site's
invalid-RUC message. The parser records it as `not_found`.

Most identity pages include a document row. A `SUCESIÓN INDIVISA` page omits
that row, so the parser takes the name from the RUC row and leaves `tipo_doc`
and `num_doc` empty. A missing document row for another contributor type is
response-shape drift and must remain a parser error.

The returned document type is not always `DNI`. Filter on `tipo_doc` before
sending identifiers on to OSIPTEL.

## `sunat_reps` response rules

The representatives flow first fetches identity information. A missing identity
is `not_found`. Some legal entities have no representatives, which is a valid
empty result. Multiple representatives produce multiple output rows for one
input RUC.
