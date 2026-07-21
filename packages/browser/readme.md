# Browser-driven site lookup

Reads sites that a plain HTTP client cannot, by driving a real Google Chrome over the
DevTools protocol on a headless server. The package is multi-site: a site-agnostic core
(a backend that launches Chrome and yields a page controller, a durable observation store,
a driven retry loop) plus one `sites/<name>/` recipe per site. Entel's debt page is the
first and, today, only site.

Input is a CSV of RUCs; output is a CSV whose columns are defined by the site. The
adjacent `<output>.state.sqlite3` keeps every observation and is the source of truth; the
CSV is a disposable projection of the latest verified row per RUC. Re-running the same
command retries whatever has not succeeded.

## How it fits together

```
cli/direct.py         parse args -> RunConfig, pick a site from the registry
run.py                ingest, open a private Xvfb, run the driven loop, export
backends/direct.py    launch Chrome with --remote-debugging-port, yield a PageController
controller.py         the backend <-> site-page seam (evaluate, open, gui_* ...)
sites/<name>/page.py   a prepared page: navigate, install JS, drive the form once
sites/<name>/parse.py  in-page payload -> LookupResult(columns)
store.py              SQLite observation log; export projects columns to CSV
```

The backend knows nothing about any site; a site page consumes only the `PageController`
protocol and never touches Chrome. Adding a site is a new `sites/<name>/` folder plus one
line in `sites/registry.py`.

## The Entel site

Entel's "Paga tu deuda" page (`https://miperfil.entel.pe/PE_Web_Cobro_Online_EU/`) is an
OutSystems Reactive app. The debt data comes from one server action,
`OnlinePayment_Step2/DataActionGetData`, gated by reCAPTCHA v3. Every rejection, whatever
its cause, comes back identical: HTTP 200 with `HasErrorDebt: true`, `DebtTotal: "0.0"`,
empty accounts, blank `DocumentNumber`. There is no error code and no score in the
response, so you cannot tell a bad token from a throttled session from a genuinely-absent
record by looking at one reply. Almost all of the difficulty is a consequence of that fact.

The app sends its debt request over `XMLHttpRequest`, not `fetch`. When you fill the form
normally it advances from the RUC screen (Step 1) to a payment screen (Step 2) and fires
the request during that transition. Tokens minted after Step 2 are rejected; tokens minted
on Step 1 are accepted. So the whole trick is to keep the page on Step 1 and mint there.

`BLOCK_STEP2_JS` (in `sites/entel/page.py`) replaces `XMLHttpRequest.prototype.send`: when
the outgoing URL contains `Step2/DataActionGetData` it parses the body, keeps it as a
template, and returns without calling the real `send`. The debt call never leaves the
browser, the spinner spins forever, and the page stays on Step 1. That hung spinner is the
working state. Driving the form once this way only captures the template; the RUC used does
not matter.

With the template captured, `INSTALL_LOOKUP_JS` runs each lookup entirely in the page:

1. `grecaptcha.execute("0", {action: "SearchDebt"})` mints a fresh v3 token. The widget id
   `"0"` is also what the request carries as `RecaptchaId`.
2. Clone the template, overwrite `screenData.variables.DocumentNumber` and
   `clientVariables.TokenCaptchaV3`.
3. `fetch` POST to the endpoint with `x-csrftoken` read from the `crf` value in `nr2Users`.
4. Read `data.HasErrorDebt` and `data.Debt.DebtTotal`.

Each lookup is one fresh token and one POST, roughly 1 to 2 seconds, no page reload. Chrome
is launched with `--remote-debugging-port` and driven by raw CDP over a websocket
(`backends/direct.py`), on a private Xvfb display with SwiftShader (`display.py`). The
one-time form drive that captures the template uses OS-level input (PyAutoGUI via XTEST)
because it must look like real typing to the input mask; the per-RUC loop after that is
pure in-page JavaScript.

## Why it rejects, and the retry policy

The reCAPTCHA v3 score fluctuates per mint, and Entel compares it to a threshold we cannot
see. A single mint from a cold, fresh-profile headless Chrome clears roughly half the time
(measured 6 of 12 on 2026-07-17), which is about what a human hand-driving the same cold
browser gets. What a real everyday Chrome profile buys is a consistently higher score; see
`packages/capture` for that path and the full reverse-engineering notes.

Because one reject is a low draw and not a fault, `run.py` re-mints a fresh token and
resends, up to `--reject-retries` (default 12), before recording a RUC as rejected. A
structured reject counts as proof the loop is alive, so `prepare()` does not restart on a
normal reject. If several RUCs in a row exhaust their whole budget the window is assumed
cold and the session restarts (`--reject-restart-threshold`, default 4;
`--max-session-restarts`, default 0). A rejected RUC is not lost: it stays `rejected` in the
store, the run exits non-zero, and a re-run retries it.

## Running it

```sh
uv run browser --input clients.csv --output entel-debts.csv --site entel \
  --binary /usr/bin/google-chrome --reject-retries 12
```

`--input` is one RUC per line. `--output` is the CSV of latest verified rows; an ambiguous
rejection never overwrites a previously verified value. Add `--diagnostics <file>.jsonl` for
redacted per-request timing and structure (values omitted, lengths kept).

On WSL, WSLg mounts `/tmp/.X11-unix` read-only, so start Xvfb with `-listen tcp -nolisten
unix` and point `DISPLAY` at `127.0.0.1:<n>`; `display.py` does this. PyAutoGUI types into
whatever window holds OS focus, so the capture drive needs its own display.

## Open problem

The token does not survive being moved to a plain HTTP client: minting in the browser and
replaying from `httpx` with the full cookie jar returns `HasErrorDebt: true` while an
in-page control in the same session passes. Something binds the token to the browser
context, so the browser cannot be dropped from the mint path, which is what caps throughput.
