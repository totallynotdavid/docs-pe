# Browser

Drives a real Chrome over the DevTools protocol for sites requiring JavaScript,
reCAPTCHA, Cloudflare, or other client-side protections. Supports manual
intervention via reCAPTCHA rejection retry and browser session restart.

```sh
uv run --env-file .env browser --input subjects.csv --output debts.csv --site entel
```

Sites that can't be driven over plain HTTP stay here. Once a site works over
HTTP (no JS, no gates), move it to [fetch](../fetch/readme.md): it's faster and
simpler. If a site's gate scores automation itself below an acceptable rate
regardless of request correctness, use [capture](../capture/readme.md) instead,
with your own established Chrome profile. See
[docs/adding-a-site.md](../../docs/adding-a-site.md).

## Supported sites

| Site           | Accepts        | Returns                                                           |
| -------------- | -------------- | ----------------------------------------------------------------- |
| `entel`        | DNI or RUC     | debt: `debt_total`, `has_punishment`                              |
| `portabilidad` | 9-digit mobile | carrier info: `receptor`, `cedente`, `asignatario_original`, etc. |

`entel` is gated by reCAPTCHA v3; `portabilidad` by Cloudflare Turnstile. Wire
protocol and gate behavior for each:
[docs/sites/entel.md](../../docs/sites/entel.md),
[docs/sites/portabilidad.md](../../docs/sites/portabilidad.md). Input is a CSV
of subjects; the classifier detects Peru mobile (9 digits starting with 9), DNI
(7 or 8 digits), and RUC (11 digits), and each subject is routed only to sites
that accept its kind.

## Command-line interface

```sh
uv run --env-file .env browser [options]
```

| Flag                         | Default                  | Notes                                                                                       |
| ---------------------------- | ------------------------ | ------------------------------------------------------------------------------------------- |
| `--site`                     | required                 | `entel` or `portabilidad`                                                                   |
| `--input`                    | required                 | Single-column CSV                                                                           |
| `--output`                   | required                 | Base filename for results                                                                   |
| `--control`                  | none                     | Warm-up identifier (must be accepted by site)                                               |
| `--reject-retries`           | 12                       | Extra token mints before recording a reject                                                 |
| `--reject-restart-threshold` | 4                        | Consecutive exhausted subjects before session restart                                       |
| `--max-session-restarts`     | 0                        | Maximum session restarts per run                                                            |
| `--proxy`                    | on                       | Use configured proxy; `--no-proxy` for local testing                                        |
| `--software-webgl`           | on                       | Use SwiftShader (consistent fingerprint, no GPU; disable for local testing with a real GPU) |
| `--state`                    | `<output>.state.sqlite3` | State database path                                                                         |
| `--diagnostics`              | off                      | Log per-request timing to JSON Lines file                                                   |

Browser uses a single session, so it takes only the first provider listed in
`PROXY_PROVIDER` and ignores lane counts.

## Configuration

Uses the same `.env` as fetch:

```env
PROXY_PROVIDER=geonode:ignored_for_browser
GEONODE_USERNAME=...
GEONODE_PASSWORD=...
```

Provider credentials and tuning: [docs/proxies.md](../../docs/proxies.md).

## Outputs and state

`<output>.state.sqlite3` stores every observation (the source of truth). The CSV
is a read-only projection of the latest verified row for each subject.
Re-running retries any subject not yet succeeded.

| File                     | Contents                  |
| ------------------------ | ------------------------- |
| `<output>.csv`           | Latest result per subject |
| `<output>.state.sqlite3` | State database            |

## The local proxy

The backend launches a browser and exposes it as a `Session` object. Each site
implements `page.py` (navigate, interact, capture responses) and `parse.py`
(convert page data into a `LookupResult`). The session protocol is
site-agnostic; sites never depend on SeleniumBase directly.

SeleniumBase's Pure CDP mode authenticates upstream proxies via CDP Fetch
interception. A Python handler intercepts requests and supplies credentials.
This works for simple pages, but Entel's OutSystems application stalls because
its subresource requests compete with the interception handler (the problem
occurs on Chrome 147-150).

`local_proxy.py` avoids interception. Chrome connects to an unauthenticated
relay on `127.0.0.1`, and the relay attaches upstream credentials itself. Each
browser session gets its own relay, so restarting the session rotates the
upstream exit without code changes.

## Rejects and retry policy

Both sites return an ambiguous rejection under normal conditions: why varies per
site (reCAPTCHA score, stale Turnstile token); see
[docs/sites/entel.md](../../docs/sites/entel.md) and
[docs/sites/portabilidad.md](../../docs/sites/portabilidad.md). Mechanically,
both are handled the same way: `RejectedError` marks a rejection, the code mints
a fresh token and retries up to `--reject-retries` times before recording the
subject as rejected. A structured reject proves the session is healthy; it never
triggers a session restart by itself. A hard `BrowserError` (e.g., crash)
propagates immediately.

If several consecutive subjects exhaust their retry budget, the session is
considered cold and restarts with a fresh proxy exit (up to
`--max-session-restarts` times). Rejected subjects aren't lost, though: they
remain in the database marked as rejected, the run exits non-zero, and a later
run retries them. See [docs/troubleshooting.md](../../docs/troubleshooting.md)
for session restarts that aren't helping, or
[why reject rates run high](../../docs/sites/entel.md#why-automation-fails) in
the first place.
