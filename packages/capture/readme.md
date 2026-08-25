# Capture

Reverse-engineer a site's request by intercepting it from your own Chrome
browser. Useful for discovering the wire protocol before automating it, and as a
fallback when automation in [browser](../browser/readme.md) can't clear a site's
gate: your own Chrome profile carries reputation an automated one doesn't have.

```sh
uv run capture --input docs.csv --output debts.csv --site entel
```

Capture is the first step when adding a new site: drive it manually in your own
Chrome, capture requests via an injected script, and learn the wire protocol. No
proxy configuration is needed. Sites are defined in
`packages/capture/capture/sites/`, each specifying its origin and capture
strategy. See [docs/adding-a-site.md](../../docs/adding-a-site.md) for the full
workflow (site definition, then implementing in browser or fetch) and
[docs/sites/entel.md](../../docs/sites/entel.md#why-automation-fails) for why
capture can end up being a site's permanent home rather than a first step.

## How it works

1. `cli.py` reads input, mints a run token, and writes
   `<output>.<site>-capture.js`: the site's capture script plus diagnostics,
   with the relay URL and token injected.
2. Start a localhost relay (`relay.py`). It owns the document queue, state
   database, and site definition. Every request is validated against the site's
   origin and run token.
3. Open the site in Chrome and paste the generated script into the DevTools
   console.
4. Complete one lookup manually so the script captures the request template.
5. Click `RUN CLIENTS` to have the script pull documents from the relay, clone
   the captured request, send it to the site, and post responses back to the
   relay.
6. The relay parses responses and stores outcomes in SQLite.

`<output>.state.sqlite3` records observations, and the CSV is the latest
verified row per document. Re-running retries anything that has not succeeded.
Add `--diagnostics <file>.jsonl` for per-request timing and structure
(redacted).

Package boundaries and the site workflow are in
[ARCHITECTURE.md](../../ARCHITECTURE.md) and
[Adding a site](../../docs/adding-a-site.md). Capture shares no code with
`browser` or `fetch`: a site moves between packages by copying knowledge, not
code, so it can be reliable here while still broken in `browser`, or working in
`browser` while it doesn't exist yet in `fetch`.
