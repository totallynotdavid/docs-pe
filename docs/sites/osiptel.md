# OSIPTEL

The OSIPTEL lookup is available at `https://checatuslineas.osiptel.gob.pe/` and
is implemented in [fetch](../../packages/cli/readme.md). It accepts any
supported DNI or RUC and returns one row per registered phone line.

## Request and response

The client warms the home page, then POSTs paginated DataTables-style requests
to:

```text
https://checatuslineas.osiptel.gob.pe/Consultas/GetAllCabeceraConsulta/
```

The request identifies DNI with `IdTipoDoc=1` and RUC with `IdTipoDoc=2`. The
client requests at most 5,000 rows per page and continues until the reported
total is covered.

Each returned line contains:

```text
modalidad, numeroServicio, operador
```

The exported result uses the columns `modalidad`, `numero`, and `operador`. The
`counts` projection groups lines by operator.

## Readiness and WAF

The home page must contain the normal site marker before a lookup starts. Treat
a block page or CAPTCHA wall as a site ban signal, including HTTP 200 responses.

OSIPTEL requires a Peru exit. Configure it in
[Proxy configuration](../proxies.md#country-and-site-selection), run the
[proxy preflight](../proxies.md#preflight), then start a small job before a
large one.

## Empty results

An empty `data` array with `iTotalRecords=0` is an `ok` lookup with no
registered lines.
