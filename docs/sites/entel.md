# Entel

`miperfil.entel.pe` "Paga tu deuda" (pay your debt) page. An OutSystems Reactive
application, debt lookup protected by reCAPTCHA v3. Implemented in
[browser](../../packages/browser/readme.md) (automated) and
[capture](../../packages/capture/readme.md) (your own Chrome profile). Not in
[fetch](../../packages/fetch/readme.md): see
[Replaying through plain HTTP](#replaying-through-plain-http) for why.

## Why lookups get rejected

Rejection is ambiguous by design: Entel's response never says _why_ a token was
refused, and the reCAPTCHA v3 score isn't exposed. Both implementations treat a
rejection as retryable, not fatal: mint a fresh token and try again, since the
same document often succeeds on a later attempt. This is what `browser`'s
`--reject-retries` and `RejectedError` handling exist for; see
[packages/browser/readme.md](../../packages/browser/readme.md).

## Rejection response

Rejected lookups return HTTP 200 with:

- `HasErrorDebt: true`
- `DebtTotal: "0.0"`
- Empty account list
- Empty `DocumentNumber`

The same response is returned for: invalid token, reused token, expired token,
empty `RecaptchaId`, invalid session. The response doesn't include an error code
or reCAPTCHA score: a single rejection doesn't reveal why. Causes must be
isolated via controlled comparison.

A successful lookup with no debt returns `HasErrorDebt: false`,
`DebtTotal: "0.0"`. Use `HasErrorDebt`, not `DebtTotal`, to distinguish
rejection from a valid zero-debt result.

## Token timing

The application sends the debt request via `XMLHttpRequest`, not `fetch`.
Hooking `window.fetch` doesn't intercept it.

Submitting the form moves the app from Step 1 (document entry) to Step 2
(payment screen). The debt request is sent during this transition:

```
Token minted on Step 1  → accepted
Token minted on Step 2  → rejected
```

The working approach:

1. Hook `XMLHttpRequest.prototype.send`
2. Capture the `Step2/DataActionGetData` request body (the template)
3. Prevent that request from being sent (intercept it)
4. Leave the app on Step 1 (don't let it move to Step 2)
5. Mint a fresh token and submit a new request from inside the page

The intercepted request leaves the loading spinner active indefinitely:
expected, the app is waiting for a blocked request.

After capturing the template, each lookup needs one fresh reCAPTCHA token, one
in-page POST, no page reload, no cleanup between requests. Lookup time:
approximately 0.8 to 1.4 seconds.

In the same browser session, removing only the Step 2 block produced 0
successful responses out of 5 attempts. The Step 2 block is necessary.

## Request endpoint

```http
POST .../screenservices/PE_Web_Cobro_Online_CW/OnlinePayment/OnlinePayment_Step2/DataActionGetData
Content-Type: application/json; charset=UTF-8
```

Relevant fields:

| Field                                 | Value                                                     |
| ------------------------------------- | --------------------------------------------------------- |
| `screenData.variables.DocumentType`   | `"RUC"` or `"DNI"`                                        |
| `screenData.variables.DocumentNumber` | Document number                                           |
| `screenData.variables.RecaptchaId`    | `"0"` (must be string "0")                                |
| `clientVariables.TokenCaptchaV3`      | Fresh reCAPTCHA token                                     |
| `viewName`                            | `"OnlinePaymentFlow.OnlinePayment"`                       |
| `versionInfo.moduleVersion`           | Retrieved from `GET .../moduleservices/moduleversioninfo` |

`DocumentType` is read as a string. A template captured with one type can be
reused for the other by replacing this field.

`RecaptchaId` must be the string `"0"` (the widget id). Blocking
`OnlinePayment_WB/DataActionGetData` causes the app to submit an empty
`RecaptchaId`, causing all debt lookups to fail even with valid tokens: that
request initializes the reCAPTCHA widget.

A manually constructed body fails for the same reason. Capture the complete body
from a live app request. The `PaymeForm` section is large but appears to have no
effect on debt lookup.

## reCAPTCHA v3

Observed values:

| Property       | Value                                      |
| -------------- | ------------------------------------------ |
| Site key       | `6LdUZwUcAAAAAC_K3DlqC_WHKbDwXfYXZrV0Xrx5` |
| Action         | `"SearchDebt"`                             |
| Widget id      | `"0"`                                      |
| Token length   | ~1300 characters                           |
| Token lifetime | ~120 seconds                               |

Site key and action confirmed by intercepting `grecaptcha.execute`.

Single-use is expected from reCAPTCHA v3 but wasn't independently measured
against Entel. A token is consumed when Entel submits it to Google for
verification; a request rejected earlier (e.g., CSRF 403) doesn't appear to
consume it.

Entel produces this browser message (expected from Entel's own duplicate-render
logic, not a blocker):

```
reCAPTCHA has already been rendered in this element
```

## CSRF

A response like this indicates a missing anti-forgery token, not a reCAPTCHA
rejection:

```http
HTTP 403

{"exception":{"message":"Invalid Login"}}
```

Bootstrap sequence:

1. GET the application root
2. GET `moduleversioninfo`
3. Send the first POST
4. Read the 403 response and its `Set-Cookie` headers
5. Extract `crf` from the `nr2Users` cookie
6. Resend with `X-CSRFToken` header

`nr2Users` is URL-encoded (`%3d` = `=`, `%3b` = `;`). Only `nr2Users` is
available via `document.cookie`. These cookies are HttpOnly and require CDP
access: `osVisit`, `osVisitor`, `nr1Users`.

## Why automation fails

A normal, previously used Chrome profile accepted documents that a fresh
automated browser rejected.

Observed 2026-07-17:

| Configuration                               | Result              |
| ------------------------------------------- | ------------------- |
| Manually driven everyday Chrome             | 6 of 6, then 5 of 5 |
| Same Chrome, Step 2 block removed           | 0 of 5              |
| Fresh automated browsers, same request flow | 0 of 5              |

This suggests acceptance depends on browser-level signals beyond the visible
request body. The exact signal remains unidentified. Whether a person or script
initiates the request isn't sufficient by itself; input method also affects
results (see below). This is why [capture](../../packages/capture/readme.md)
exists, to use an established browser profile.

## Ruled-out variables

These were tested without changing acceptance:

- TLS client choice (`httpx`, `curl_cffi`, in-page path)
- `navigator.webdriver` and common stealth properties
- Windows Chrome User-Agent and UA-CH values
- Intel WebGL vendor/renderer values
- `deviceMemory` and `languages`
- Hardware GPU (SwiftShader sufficient)
- The `_GRECAPTCHA` cookie (copying from a working browser didn't transfer
  acceptance)
- Exit IP (two Peru residential exits failed; working browser used host IP)
- Cookie clearing and profile reuse
- Token length, uniqueness, mint time
- Human versus programmatic form interaction within same browser

Measured rejected tokens: length 1316-1358 characters, mint time 164-255 ms,
distinct prefix on each request. These were fresh, unspent tokens, yet Entel
rejected them.

## Replaying through plain HTTP

A token minted in the browser couldn't be successfully replayed through `httpx`.

Controlled sequence: block Step 2 XHR, capture exact app request body, preserve
fresh unspent token, export complete browser cookie jar via CDP, replay via
`httpx`.

Result: `HasErrorDebt: true` (rejection). Same session/IP, normal in-browser
request seconds later succeeded with real debt. The session and IP were still
valid.

The flow "mint in browser, request via `httpx`" doesn't work without reproducing
an additional browser-bound property. Whether that's TLS-related remains
unresolved. **This is why Entel stays in `browser` instead of moving to
`fetch`**: the token cannot be reused from a plain HTTP client, so a browser has
to remain part of the request path.

## CDP input vs OS-level input

Controlled comparison (same browser, site, form, IP):

| Input method               | Result               |
| -------------------------- | -------------------- |
| CDP with `isTrusted: true` | `HasErrorDebt: true` |
| PyAutoGUI through X11      | Accepted, 3 of 3     |

A CDP-generated click reports `isTrusted: true`, but that doesn't make it
equivalent to OS-level input. The OutSystems input mask also treated input
methods differently: CDP `send_keys` entered only the first character; PyAutoGUI
entered the complete value.

## Development environment notes

These were found while investigating the input-method difference above; they
apply to any worker driving Entel through PyAutoGUI/Xvfb, not just the initial
investigation.

**Display and input:** Xvfb is sufficient: a PyAutoGUI-driven form on a headless
virtual display returned real debt, and XTEST input through Xvfb was accepted.
PyAutoGUI sends input to whichever window has OS focus; sharing a display
between sessions can silently corrupt runs (input containing only one character,
input receiving no text, another window receiving keystrokes). **Each worker
should use its own display.**

PyAutoGUI uses screen coordinates. Elements below the viewport should be
centered first:

```javascript
element.scrollIntoView({ block: "center" });
```

On a 1080p display, dropdown options rendered near `y=1157` before scrolling.

**WSL:** SeleniumBase's `xvfb=True` doesn't work under WSLg (tested
environment). WSLg mounts `/tmp/.X11-unix` read-only without the sticky bit; a
new X server can't create a Unix socket there, and `chmod` can't change the
directory. Start Xvfb manually instead:

```sh
Xvfb :99 -screen 0 1920x1080x24 -listen tcp -nolisten unix
export DISPLAY=127.0.0.1:99
```

Then run SeleniumBase with `headed=True`. `-nolisten unix` prevents Xvfb from
accessing `/tmp/.X11-unix`. `XAUTHORITY` must point to an existing file (may be
empty).

**SeleniumBase display size:** on normal Linux, `SB(uc=True, xvfb=True)` works.
One known issue: `activate_cdp_mode()` creates a second virtual display without
forwarding `xvfb_metrics`, so `xvfb_metrics = "1920,1080"` may still produce a
1366x768 display, and PyAutoGUI then fails when clicking outside that screen.
Documented in SeleniumBase discussion
[#3664](https://github.com/seleniumbase/SeleniumBase/discussions/3664).

UC mode also removes the proxy authentication extension, so an authenticated
upstream proxy produces blank pages. `browser/local_proxy.py` handles this with
a local unauthenticated relay: see
[packages/browser/readme.md](../../packages/browser/readme.md#the-local-proxy).

## See also

- [packages/browser/readme.md](../../packages/browser/readme.md): automated
  Entel lookup, reject-retry flags
- [packages/capture/readme.md](../../packages/capture/readme.md): using your own
  Chrome profile for Entel
- [architecture.md](../architecture.md): system overview
