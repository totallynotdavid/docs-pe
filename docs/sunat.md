# SUNAT

The SUNAT lookup is available at `https://e-consultaruc.sunat.gob.pe/` and is
implemented in [browser](../packages/browser/readme.md). It is not implemented
in [fetch](../packages/cli/readme.md): the host answers every plain HTTP request
with a Cloudflare JavaScript challenge (`403`, `cf-mitigated: challenge`). The
`sunat` and `sunat_reps` sites accept different RUC kinds: `sunat` takes a
natural person's RUC (not starting with 20) and `sunat_reps` takes a legal
entity's RUC (starting with 20).

```sh
uv run --env-file .env browser \
  --input rucs.csv \
  --output results/sunat.csv \
  --site sunat_reps
```

No other SUNAT host returns the same fields. The `ww1.sunat.gob.pe` company
endpoint gives only the name and address of a legal entity, and the reduced RUC
register is a bulk file without representatives or document types.

## Sites and output

Each output row starts with a `subject` column and ends with `observed_at`.

| Site         | Input              | Output columns                                                                                       |
| ------------ | ------------------ | ---------------------------------------------------------------------------------------------------- |
| `sunat`      | Natural-person RUC | `tipo_doc`, `num_doc`, `nombre`, `tipo_contribuyente`.                                               |
| `sunat_reps` | Legal-entity RUC   | `razon_social`, `doc_type`, `num_doc`, `nombre`, `cargo`, `fecha_desde`, one row per representative. |

An unregistered RUC, and a legal entity without representatives, are recorded as
successful lookups and produce no output row.

## Browser sequence

1. Chrome opens the search page, `FrameCriterioBusquedaWeb.jsp`. Cloudflare
   clears the browser there; the sequence waits up to 60 seconds for the search
   button, and a browser that never sees it is a hard browser error.
2. The page sends a synchronous `XMLHttpRequest` form POST to `jcrS00Alias` with
   `accion=consPorRuc`. The request includes a random token with the shape
   expected by the site's client-side wrapper. The server does not validate it
   as a reCAPTCHA token. The request reuses the page's clearance cookie.
3. `sunat_reps` then sends `accion=getRepLeg` for the representatives.

An HTTP status other than 200 is a hard browser error. The parsers in
`browser/sites/sunat/parse.py` read the returned HTML.

## `sunat` response rules

An unregistered RUC returns an ordinary result page containing the site's
invalid-RUC message. The parser records it as an empty result.

Most identity pages include a document row. A `SUCESIÓN INDIVISA` page omits
that row, so the parser takes the name from the RUC row and leaves `tipo_doc`
and `num_doc` empty. A missing document row for another contributor type is
response-shape drift and must remain a parser error.

The returned document type is not always `DNI`. Filter on `tipo_doc` before
sending identifiers on to OSIPTEL.

## `sunat_reps` response rules

The representatives flow first reads the name from the RUC row of the identity
page; a legal entity page has no document row. An unregistered RUC is an empty
result, and the representatives request is not sent.

Some legal entities have no representatives. SUNAT answers with its generic
error page, "Pagina de Error", containing "No se encontro información para
representantes legales". That is a valid empty result, so the parser checks the
marker before it treats the error page as drift. Multiple representatives
produce multiple output rows for one input RUC.
