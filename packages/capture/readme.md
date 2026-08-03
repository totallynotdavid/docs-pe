# capture

The tool to reach for first when adding a site. It intercepts a site's own calls
from your everyday Chrome, so you can learn its request recipe, and it collects
through that reputable browser when an automated one cannot clear the gate.

```sh
uv run capture --input docs.csv --output debts.csv --site entel
```

It launches no browser and depends on nothing outside the standard library. It
stands up a localhost relay, prints a script you paste into DevTools, and
records what the page sends back.

## How a run works

1. `cli.py` ingests the documents, mints a run token, and writes
   `<output>.<site>-capture.js`: the site's `capture.js` plus shared page
   diagnostics, with the relay URL and token injected.
2. `relay.py` serves a localhost `HTTPServer`. `RelayState` holds the document
   queue, the store, and the selected site. Every request is checked against the
   site's `origin` and the run token, so a stray tab cannot drive it.
3. You open the site in your Chrome, paste the script, drive the form once (the
   XHR hook captures the request template, and the spinner hangs, which is the
   working state), then click RUN CLIENTS.
4. The in-page loop pulls the next document from `/next`, mints a token, clones
   the template, POSTs to the site endpoint, and returns the JSON to `/result`.
   `RelayState.record` parses it through the site and writes an observation.

Durability matches `browser`: `<output>.state.sqlite3` is the source of truth,
the CSV is the latest verified row per document, and a re-run retries what did
not succeed. There is no retry budget, because a reputable browser clears most
documents on the first mint and a re-run handles the rest.

Add `--diagnostics <file>.jsonl` for redacted per-request timing and structure.

## Why a reputable browser

Entel's debt page gates one server action behind reCAPTCHA v3 and answers every
rejection identically. The mechanism, the Step 1 mint, and the XHR block are the
same as in `packages/browser`. The difference is the browser.

The v3 score fluctuates per mint, and a real everyday Chrome profile scores
consistently higher than a cold headless one, so it clears the borderline
documents automation struggles with. Hand-driven real Chrome returned 6 of 6 and
then 5 of 5 on 2026-07-17, against 0 of 5 for a cold automated browser running
the identical recipe across every profile and exit tried.

Whether a person or a script clicks makes no difference to the score. What
matters is the browser Google has actually observed over time, and that
reputation is bound server-side: it does not travel in a copied cookie.

Everything tested and ruled out, the endpoint contract, and the open questions
are in [docs/entel.md](../../docs/entel.md).

## Its relationship to the other packages

This package is independent of `packages/browser`. Knowledge crosses between
them through you: discover a site here, then hand-author its automated recipe
there. Both hold their own copy of the site's parser, columns, and endpoint.

`ruc.py` keeps its own document type for the same reason. No lookup here routes
by kind, so it has no kind field.
