# Capture: discovery through a reputable browser

The tool you reach for first when adding a site. It intercepts a site's own calls from your
everyday Chrome, so you can learn its request recipe, and it collects through that reputable
browser when automated Chrome cannot clear the gate. It launches no browser and depends on
nothing outside the standard library: it stands up a localhost relay, prints a script you
paste into DevTools, and records what the page sends back.

This package is deliberately independent of `packages/browser`. Knowledge crosses between
them through you, not through an import: discover a site here, then hand-author its automated
recipe in `browser`. Both hold their own copy of the site's parser, columns, and endpoint.

## How a run works

```
capture --input rucs.csv --output debts.csv --site entel [--port 8765]
```

1. `cli.py` ingests the RUCs, mints a token, and writes `<output>.<site>-capture.js`
   (the site's `capture.js` plus shared page diagnostics, with the relay URL and token
   injected).
2. `relay.py` serves a localhost `HTTPServer`. `RelayState` holds the RUC queue, the store,
   and the selected site; every request is checked against the site's `origin` and the token.
3. You open the site in your Chrome, paste the script, drive the RUC form once (the XHR hook
   captures the request template; the spinner hangs, which is the working state), and click
   RUN CLIENTS.
4. The in-page loop pulls the next RUC from `/next`, mints a token, clones the template,
   POSTs to the site endpoint, and returns the JSON to `/result`. `RelayState.record` parses
   it through the site and writes an observation.

Output and durability match `browser`: `<output>.state.sqlite3` is the source of truth, the
CSV is the latest verified row per RUC, and a re-run retries what did not succeed. There is
no retry budget here; a reputable browser clears most RUCs on the first mint, and a re-run
handles the rest.

## The Entel site, and why a reputable browser matters

Entel's debt page gates one server action behind reCAPTCHA v3 and answers every rejection
identically (HTTP 200, `HasErrorDebt: true`, blank debt). The mechanism, the Step-1 mint,
and the XHR block are the same as in `packages/browser`; the difference is the browser. The
v3 score fluctuates per mint, and a real everyday Chrome profile scores consistently higher
than a cold headless one, so it clears the borderline RUCs automation struggles with.

Measured on 2026-07-17: a real hand-driven Chrome cleared 6/6 then 5/5, and 0/5 with the
block removed in the same browser and hour. A cold automated browser with the same recipe
scored 0/5 across every profile and exit tried. Whether a person or a script clicks makes no
difference to the score; what matters is the browser Google has actually observed over time.

## What was ruled out

Each was tested directly and changed nothing about acceptance:

- TLS fingerprint (`httpx`, `curl_cffi` behaved the same as the in-page path).
- `navigator.webdriver` and the usual stealth surface, via playwright-stealth.
- User-Agent and UA-CH brand strings spoofed to Windows Chrome.
- WebGL vendor/renderer spoofed to Intel, plus `deviceMemory` and `languages`.
- Real GPU (none under WSL anyway; SwiftShader is sufficient).
- The `_GRECAPTCHA` cookie. Injecting a real high-reputation browser's cookie survived on
  every domain yet scored 0/6 while a real Chrome passed the same RUCs in the same minute.
  Reputation is bound server-side to the browser Google observed; it does not travel in a
  copied cookie value.
- Human vs programmatic input in the same browser: same intermittent rate.
- CDP input specifically is distinguishable. A CDP click reports `isTrusted: true` and is
  still rejected; the OutSystems input mask also drops CDP `send_keys` (only the first
  character lands) but accepts PyAutoGUI keystrokes. `isTrusted` is not sufficient.

The disposable-key reCAPTCHA harness is not diagnostic here: an untrained free-tier key
returns coarse buckets and reported 0.9 for every configuration. It cannot see Entel's
trained model.

## Endpoint facts worth keeping

- POST to `.../OnlinePayment_Step2/DataActionGetData`,
  `Content-Type: application/json; charset=UTF-8`.
- Success: `{"data":{"Debt":{"DocumentNumber","DebtTotal","Accounts":{"List":[...]},
  "HasPunishment"},"HasErrorDebt":false}}`. `HasErrorDebt: false` with `DebtTotal "0.0"`
  means genuinely no debt. The flag decides, not the amount.
- `RecaptchaId` must be `"0"` and is validated server-side. The template must come from the
  real form; a hand-built body with `RecaptchaId: ""` fails even with a valid token.
- CSRF: the first POST of a session returns 403 `{"exception":{"message":"Invalid Login"}}`
  and sets `nr2Users`. Read `crf` from it and resend with header `X-CSRFToken`. A 403 is a
  CSRF problem, not a captcha problem, and does not consume the token.
- reCAPTCHA action is `"SearchDebt"`. Tokens are about 1300 characters, expire around 120
  seconds, and are assumed single-use.
- The large `PaymeForm` block in the request body appears inert. Capture it from a live
  request rather than hand-build it.

Fuller field notes live in `docs/findings.txt`.

## Running it

```sh
uv run capture --input clients.csv --output entel-debts.csv --site entel
```

Add `--diagnostics <file>.jsonl` for redacted per-request timing and structure. The relay
serves only localhost and accepts requests only from the site's origin bearing the run's
token, so a stray tab cannot drive it.
