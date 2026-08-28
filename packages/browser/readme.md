# browser

`browser` drives Chrome through the DevTools protocol for sites that require
JavaScript, reCAPTCHA, Cloudflare, or a browser-level reputation signal.

```sh
uv run --env-file .env browser \
  --input subjects.csv \
  --output results/entel.csv \
  --site entel
```

Use [fetch](../cli/readme.md) when a plain HTTP client is sufficient. Use
[capture](../capture/readme.md) when a request works in an established Chrome
profile but not in automation.

## Sites

| Site           | Accepted input           | Output                               |
| -------------- | ------------------------ | ------------------------------------ |
| `entel`        | DNI or RUC               | Debt total and punishment status.    |
| `portabilidad` | Nine-digit mobile number | Carrier history and current carrier. |

The [site notes](../../docs/sites/) own each site's endpoint, browser gate, and
response contract.

## Sessions and retries

The state database defaults to `<output>.state.sqlite3`. It records every
observation, including `ok`, `rejected`, and `failed`. Only `ok` marks a subject
complete, so reusing the same output and state paths resumes subjects that have
not succeeded.

A structured site rejection causes a fresh token or session attempt according to
`--reject-retries`, `--reject-restart-threshold`, and `--max-session-restarts`.
A hard browser error is not converted into a document result. It stops the run
so the browser failure can be fixed.

Entel has a browser-bound request sequence and must remain in this package. The
details are in [the Entel note](../../docs/sites/entel.md). Portabilidad does
not use `--control`; the flag is for sites that need a warm-up identifier.

## Proxy mode

Browser creates one local unauthenticated relay for Chrome and attaches proxy
credentials outside the browser. A session restart creates a new relay and
upstream session. Browser uses the first provider in `PROXY_PROVIDER` and does
not use provider lane counts. See [Proxy configuration](../../docs/proxies.md)
for credentials and country settings.

## Command reference

Run the executable for the complete, current option list:

```sh
uv run browser --help
```

Use `--diagnostics` for redacted JSON Lines timing data. Do not treat the
diagnostic file or the result CSV as the state ledger.
