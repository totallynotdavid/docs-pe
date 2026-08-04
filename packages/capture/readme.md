# capture

The first tool to reach for when adding a site. It intercepts a site's own
requests from a real Chrome, making it possible to learn the request recipe and
collect through that browser when automation cannot clear the gate.

```sh
uv run capture --input docs.csv --output debts.csv --site entel
```

It launches no browser and depends only on the standard library. It starts a
localhost relay, prints a script to paste into DevTools, and records the page's
responses.

## How a run works

1. `cli.py` ingests the documents, mints a run token, and writes
   `<output>.<site>-capture.js`: the site's `capture.js` plus shared page
   diagnostics, with the relay URL and token injected.
2. `relay.py` starts a localhost `HTTPServer`. `RelayState` owns the document
   queue, the store, and the selected site. Every request is checked against the
   site's `origin` and the run token.
3. Open the site in Chrome, paste the script, complete one lookup so the XHR
   hook captures the request template, then click **RUN CLIENTS**.
4. The page pulls documents from `/next`, mints a token, clones the captured
   request, sends it to the site, and posts the response to `/result`.
   `RelayState.record` parses it and stores the observation.

Durability matches `browser`: `<output>.state.sqlite3` is the source of truth,
the CSV is the latest verified row per document, and a re-run retries anything
that did not succeed.

Add `--diagnostics <file>.jsonl` for redacted per-request timing and structure.

## Why a real browser

Entel's debt page protects one server action with reCAPTCHA v3 and returns the
same response for every rejection. The request flow is identical to
`packages/browser`; only the browser changes.

A real Chrome profile consistently earns higher reCAPTCHA v3 scores than a cold
headless browser, allowing it to clear borderline documents that automation
cannot. On 2026-07-17, a hand-driven Chrome completed 6 of 6 and then 5 of 5
lookups, while a cold automated browser completed 0 of 5 using the same request
recipe across every profile and proxy tested.

The score depends on the browser's accumulated reputation, not on whether a
person or a script clicks. That reputation is evaluated server-side and cannot
be copied with cookies.

The endpoint contract, failed experiments, and remaining open questions are
documented in [docs/entel.md](../../docs/entel.md).

## Relationship to the other packages

This package is independent of `packages/browser`. Sites move between them by
copying knowledge, not code: discover the request recipe here, then implement it
there. Each package owns its own parser, columns, and endpoint logic.
