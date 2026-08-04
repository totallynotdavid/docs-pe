# browser

Drives a real Chrome over the DevTools protocol for sites that cannot yet be
driven over plain HTTP. Each site lives in its own `sites/<name>/` recipe on top
of a site-agnostic core.

```sh
uv run --env-file .env browser --input subjects.csv --output debts.csv --site entel --control <ruc>
```

Sites move to [`fetch`](../fetch/readme.md) once they no longer require a
browser.

## Sites

| Site           | Accepts        | Returns                                                                                     |
| -------------- | -------------- | ------------------------------------------------------------------------------------------- |
| `entel`        | DNI or RUC     | `debt_total`, `has_punishment`                                                              |
| `portabilidad` | 9-digit mobile | `receptor`, `cedente`, `asignatario_original`, `fecha_ventana`, `estado`, `current_carrier` |

`entel` reads Entel's "Paga tu deuda" page, which is gated by reCAPTCHA v3.
`portabilidad` reads `consulta.portabilidad.pe`, which is gated by Cloudflare
Turnstile. Those gates are why this package exists.

`--control` is a warm-up identifier for sites that need one. Entel uses it to
capture its request template and health-check the session. Portabilidad ignores
it.

## How it fits together

```text
cli/direct.py
    Parse CLI arguments, build a RunConfig, and select a site.

run.py
    Coordinate the run: read input, resolve the proxy, execute retries,
    and export results.

backends/
    Launch a browser and expose it as a Session.

session.py
    Common browser interface used by every site.

sites/<name>/
    page.py   Drive the site.
    parse.py  Convert the page payload into a LookupResult.

local_proxy.py
    Local relay that exposes an authenticated proxy without credentials.

store.py
    Record observations in SQLite and export selected columns to CSV.
```

The backend knows nothing about any site. Site pages consume only the `Session`
protocol and never depend on SeleniumBase directly. Adding a site is a new
`sites/<name>/` directory plus one entry in `sites/registry.py`.

Input is a CSV of subjects. `subject.py` classifies each by digit shape, and the
lengths never collide: a Peru mobile is 9 digits beginning with 9, a DNI is 7 or
8 digits, and a RUC is 11. The planner routes each subject only to sites that
accept its kind.

Each site defines its own output columns.

`<output>.state.sqlite3` stores every observation and is the source of truth.
The CSV is a disposable projection of the latest verified row for each subject.
Re-running retries any subject that has not yet succeeded.

## The local proxy

SeleniumBase's Pure CDP mode authenticates upstream proxies by enabling CDP
Fetch interception. Every request pauses while a Python handler supplies proxy
credentials.

That works for simple pages. Entel's OutSystems application stalls because its
subresource requests compete with the interception handler. The problem
reproduces on Chrome 147 through 150.

`local_proxy.py` avoids interception entirely. Chrome connects to an
unauthenticated relay on `127.0.0.1`, and the relay attaches the upstream
credentials itself. Each browser session gets its own relay, so restarting the
session still rotates the upstream exit.

## Rejects and the retry policy

Both sites return an ambiguous rejection on an otherwise healthy session.
Entel's reCAPTCHA v3 score varies from token to token, and portabilidad's
Turnstile token eventually expires.

`RejectedError` owns that decision. On rejection, `run.py` mints a fresh token
and retries up to `--reject-retries` before recording the subject as rejected. A
structured reject proves the loop is healthy, so it never triggers a session
restart on its own. A hard `BrowserError` propagates immediately.

If several consecutive subjects exhaust their retry budget, the session is
considered cold and restarts with a fresh proxy exit.

Rejected subjects are not lost. They remain rejected in the store, the run exits
non-zero, and a later run retries them.

| flag                         | default                  | notes                                                   |
| ---------------------------- | ------------------------ | ------------------------------------------------------- |
| `--site`                     | required                 | `entel` or `portabilidad`                               |
| `--control`                  | none                     | warm-up identifier; must be accepted by the site        |
| `--reject-retries`           | 12                       | extra token mints before recording a reject             |
| `--reject-restart-threshold` | 4                        | consecutive exhausted subjects before a session restart |
| `--max-session-restarts`     | 0                        |                                                         |
| `--proxy`                    | on                       | `--no-proxy` for a direct local run                     |
| `--software-webgl`           | on                       | SwiftShader, for a consistent fingerprint with no GPU   |
| `--state`                    | `<output>.state.sqlite3` |                                                         |
| `--diagnostics`              | off                      | redacted per-request timing and structure as JSON Lines |

Browser uses a single session, so it always takes the first provider listed in
`PROXY_PROVIDER` and ignores lane counts. The shared root `.env` is loaded by
UV, so it works unchanged for `fetch` and `browser`.

## What limits throughput

Entel's token cannot be reused from a plain HTTP client, so the browser remains
part of the request path. That is why Entel stays in `browser` instead of
`fetch`.

The full investigation, including everything that was ruled out, is in
[docs/entel.md](../../docs/entel.md). Read it before changing the Entel recipe:
most ideas that look promising have already been tried and measured.
