# Browser

Drives Chrome over the DevTools protocol for sites requiring JavaScript,
reCAPTCHA, Cloudflare, or another browser gate.

```sh
uv run --env-file .env browser \
  --input subjects.csv \
  --output results.csv \
  --site entel
```

Use [fetch](../fetch/readme.md) when a site works over plain HTTP. Use
[capture](../capture/readme.md) when the site's gate accepts an established
Chrome profile but rejects automation.

## Sites

| Site           | Input          | Output                                                                |
| -------------- | -------------- | --------------------------------------------------------------------- |
| `entel`        | DNI or RUC     | Debt: `debt_total`, `has_punishment`                                  |
| `portabilidad` | 9-digit mobile | Carrier data: `receptor`, `cedente`, `asignatario_original`, and more |

The classifier routes each subject only to sites that accept its document kind.
Site-specific wire behavior and gate handling live in the
[site notes](../../docs/sites/).

## Configuration

Browser uses the first provider in `PROXY_PROVIDER` and one proxy session for
the run. Lane counts are ignored.

```env
PROXY_PROVIDER=geonode
GEONODE_USERNAME=...
GEONODE_PASSWORD=...
```

Provider credentials and country settings are documented in
[Proxy configuration](../../docs/proxies.md).

## Command-line interface

| Flag                         | Default                  | Notes                                                                 |
| ---------------------------- | ------------------------ | --------------------------------------------------------------------- |
| `--control`                  | none                     | Warm-up identifier; must be accepted by the site                      |
| `--reject-retries`           | 12                       | Extra token mints before recording a reject                           |
| `--reject-restart-threshold` | 4                        | Consecutive exhausted subjects before a session restart               |
| `--max-session-restarts`     | 0                        | Session restarts allowed per run                                      |
| `--proxy` / `--no-proxy`     | on                       | Use the configured proxy; disable for local testing                   |
| `--software-webgl`           | on                       | Use SwiftShader for a consistent fingerprint; disable with a real GPU |
| `--state`                    | `<output>.state.sqlite3` | State database path                                                   |
| `--diagnostics`              | off                      | Append redacted per-request timing to a JSON Lines file               |

`uv run browser --help` prints this same table.

## State and retries

`<output>.state.sqlite3` records observations; the CSV contains the latest
verified result for each subject. A later run retries subjects that did not
succeed. See [Architecture](../../ARCHITECTURE.md) for the shared state model.

Both sites can reject an otherwise valid lookup because their browser token or
session is stale. Browser raises `RejectedError`, mints a fresh token, and
retries up to `--reject-retries`. A structured rejection proves the session is
healthy and does not restart it by itself; a hard `BrowserError` (a crash, for
example) propagates immediately instead of retrying. Repeated exhausted
rejections can trigger a fresh session according to `--reject-restart-threshold`
and `--max-session-restarts`.

Entel's complete request and token constraints are in
[its site note](../../docs/sites/entel.md).

Read [Troubleshooting](../../docs/operations/troubleshooting.md) when session
restarts are not improving the rejection rate.

## The local proxy

Each browser session gets its own local relay (`local_proxy.py`): Chrome talks
to an unauthenticated `127.0.0.1` endpoint, and the relay attaches the upstream
proxy's credentials itself. A session restart gets a fresh relay and therefore a
fresh upstream exit, with no code changes. See
[why Entel needs this](../../docs/sites/entel.md#why-automation-fails) for the
CDP interception failure this works around.
