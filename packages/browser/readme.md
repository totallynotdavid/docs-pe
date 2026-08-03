# browser

Reads sites that a plain HTTP client cannot, by driving a real Chrome over the
DevTools protocol on a headless display. One site-agnostic core plus one
`sites/<name>/` recipe per site.

```sh
uv run browser --input subjects.csv --output debts.csv --site entel --control <ruc>
```

Its sites are absent from the portal by design: a site lands here while it still
needs a browser, and moves to [`fetch`](../fetch/readme.md) once it can be
driven over plain HTTP.

## Sites

| Site           | Accepts        | Returns                                                                                     |
| -------------- | -------------- | ------------------------------------------------------------------------------------------- |
| `entel`        | DNI or RUC     | `debt_total`, `has_punishment`                                                              |
| `portabilidad` | 9-digit mobile | `receptor`, `cedente`, `asignatario_original`, `fecha_ventana`, `estado`, `current_carrier` |

`entel` reads Entel's "Paga tu deuda" page, which is gated by reCAPTCHA v3.
`portabilidad` reads `consulta.portabilidad.pe`, which is gated by Cloudflare
Turnstile. Both gates are why this package exists.

`--control` is a warm-up identifier for sites that need one. Entel uses it to
capture its request template and to health-check the session; portabilidad
ignores it.

## How it fits together

```
cli/direct.py           parse args -> RunConfig, pick a site from the registry
run.py                  ingest, resolve the proxy, drive the retry loop, export
backends/seleniumbase.py  launch a SeleniumBase Pure CDP browser, yield a Session
session.py              the backend/site-page seam (evaluate, open, gui_click ...)
local_proxy.py          an unauthenticated 127.0.0.1 relay onto an authenticated proxy
sites/<name>/page.py    a prepared page: navigate, install JS, drive the form
sites/<name>/parse.py   in-page payload -> LookupResult(columns)
store.py                SQLite observation log; export projects columns to CSV
```

The backend knows nothing about any site, and a site page consumes only the
`Session` protocol and never touches SeleniumBase. Adding a site is a new
`sites/<name>/` folder plus one entry in `sites/registry.py`.

Input is a CSV of subjects. `subject.py` classifies each by digit shape, and the
lengths never collide: a Peru mobile is 9 digits leading 9, a DNI is 7 or 8, a
RUC is 11. The planner routes a subject only to a site that accepts its kind.

Output is a CSV whose columns the site defines. The adjacent
`<output>.state.sqlite3` holds every observation and is the source of truth; the
CSV is a disposable projection of the latest verified row per subject.
Re-running retries whatever has not succeeded.

## The local proxy

SeleniumBase's Pure CDP mode authenticates an upstream proxy by enabling CDP
Fetch interception, which pauses and resumes every request through a Python
handler on the CDP event loop. One simple request survives that. A heavy SPA
does not: Entel's OutSystems app stalls and never renders, because the
interception starves its subresource XHRs.

Downgrading Chrome does not help: the Fetch path has no version gate, and the
blank-render stall reproduces on Chrome 147, 148, 149, and 150 alike.

So `local_proxy.py` terminates the auth locally. Chrome talks to an
unauthenticated `127.0.0.1` relay, no interception is ever enabled, and the
relay attaches the upstream credentials itself. One relay per browser session,
so a session restart still rotates the upstream exit.

## Rejects and the retry policy

Both sites answer an ambiguous rejection on a healthy session: Entel's reCAPTCHA
v3 score fluctuates per mint, and portabilidad's Turnstile token goes stale. A
single mint clears only some of the time.

`RejectedError` is the sole owner of that verdict, and it is what makes the
retry policy work: on a reject, `run.py` re-mints a fresh token and resends, up
to `--reject-retries`, before recording the subject as rejected. A structured
reject also proves the loop is alive, so it never triggers a session restart on
its own. A hard `BrowserError` propagates immediately.

If several subjects in a row exhaust their whole budget, the window is assumed
cold and the session restarts, which mints a fresh proxy exit. A rejected
subject is not lost: it stays rejected in the store, the run exits non-zero, and
a re-run retries it.

| flag                         | default                  | notes                                                   |
| ---------------------------- | ------------------------ | ------------------------------------------------------- |
| `--site`                     | required                 | `entel` or `portabilidad`                               |
| `--control`                  | none                     | warm-up identifier; must be one the site accepts        |
| `--reject-retries`           | 12                       | extra token mints before recording a reject             |
| `--reject-restart-threshold` | 4                        | consecutive exhausted subjects before a session restart |
| `--max-session-restarts`     | 0                        |                                                         |
| `--proxy`                    | on                       | `--no-proxy` for a direct local run                     |
| `--env-file`                 | `.env`                   | same `PROXY_PROVIDER` contract as `fetch`               |
| `--software-webgl`           | on                       | SwiftShader, for a consistent fingerprint with no GPU   |
| `--state`                    | `<output>.state.sqlite3` |                                                         |
| `--diagnostics`              | off                      | redacted per-request timing and structure as JSON Lines |

Browser drives one session at a time, so it takes the first provider named in
`PROXY_PROVIDER` and ignores any lane count. A shared `.env` listing several
providers for `fetch`'s pool works here unchanged.

## What limits throughput

Entel's token does not survive being moved to a plain HTTP client, so the
browser cannot be dropped from the mint path. That is the ceiling on this site,
and it is why Entel is not in `fetch`.

The full investigation, including everything ruled out, is in
[docs/entel.md](../../docs/entel.md). Read it before changing the Entel recipe:
most of what looks worth trying has already been tried and measured.
