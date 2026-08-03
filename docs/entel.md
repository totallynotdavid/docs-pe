# Entel debt lookup

Internal notes for `miperfil.entel.pe/PE_Web_Cobro_Online_EU/` ("Paga tu
deuda").

The behavior documented here was tested through both
[`packages/browser`](../packages/browser/readme.md) and
[`packages/capture`](../packages/capture/readme.md).

The site is an OutSystems Reactive application. Debt lookup is handled by a
server action protected by reCAPTCHA v3.

## Rejection response

Rejected lookups return HTTP 200 with:

- `HasErrorDebt: true`
- `DebtTotal: "0.0"`
- an empty account list
- an empty `DocumentNumber`

The same response is returned for:

- an invalid token
- a reused token
- an expired token
- an empty `RecaptchaId`
- an invalid session

The response does not include an error code or reCAPTCHA score. A single
rejected request therefore does not reveal why it failed. Causes must be
isolated through controlled comparisons.

A successful lookup with no debt returns:

```text
HasErrorDebt: false
DebtTotal: "0.0"
```

Use `HasErrorDebt`, not `DebtTotal`, to distinguish rejection from a valid
zero-debt result.

## Token timing

The application sends the debt request through `XMLHttpRequest`, not `fetch`.
Hooking `window.fetch` does not intercept it.

Submitting the form normally moves the application from the document screen,
Step 1, to the payment screen, Step 2. The debt request is sent during this
transition.

```text
Token minted while the page remains on Step 1  -> accepted
Token minted after the page reaches Step 2     -> rejected
```

The working approach is:

1. Hook `XMLHttpRequest.prototype.send`.
2. Capture the `Step2/DataActionGetData` request body.
3. Prevent that request from being sent.
4. Leave the application on Step 1.
5. Mint a fresh token and submit a new request from inside the page.

The intercepted request leaves the loading spinner active indefinitely. This is
expected: the application is waiting for a request that was intentionally
blocked.

The first form submission is only used to capture the request template. The
document entered during that submission does not matter.

After capturing the template, each lookup requires:

- one fresh reCAPTCHA token
- one in-page POST
- no page reload
- no cleanup between requests

Observed lookup time was approximately 0.8 to 1.4 seconds.

In the same browser session, removing only the Step 2 block produced 0
successful responses out of 5 attempts.

## Endpoint

```http
POST .../screenservices/PE_Web_Cobro_Online_CW/OnlinePayment/OnlinePayment_Step2/DataActionGetData
Content-Type: application/json; charset=UTF-8
```

Relevant request fields:

| Field                                 | Value                                                     |
| ------------------------------------- | --------------------------------------------------------- |
| `screenData.variables.DocumentType`   | `"RUC"` or `"DNI"`                                        |
| `screenData.variables.DocumentNumber` | Document number                                           |
| `screenData.variables.RecaptchaId`    | `"0"`                                                     |
| `clientVariables.TokenCaptchaV3`      | Fresh reCAPTCHA token                                     |
| `viewName`                            | `"OnlinePaymentFlow.OnlinePayment"`                       |
| `versionInfo.moduleVersion`           | Retrieved from `GET .../moduleservices/moduleversioninfo` |

`DocumentType` is read as a plain string. A template captured with one document
type can be reused for the other by replacing this field.

`RecaptchaId` must be the string `"0"`. It matches the reCAPTCHA widget id.

Blocking `OnlinePayment_WB/DataActionGetData` causes the application to submit
an empty `RecaptchaId`. The debt lookup then fails even when the token was
minted by the real application. That request appears to initialize the widget.

A manually constructed body fails for the same reason. Capture the complete
request body from a live application request instead.

The large `PaymeForm` section appears to have no effect on the debt lookup.

## reCAPTCHA

Observed values:

- Site key: `6LdUZwUcAAAAAC_K3DlqC_WHKbDwXfYXZrV0Xrx5`
- Action: `"SearchDebt"`
- Widget id: `"0"`
- Token length: approximately 1300 characters
- Token lifetime: approximately 120 seconds

The site key and action were confirmed by intercepting `grecaptcha.execute`.

Single-use behavior is expected from reCAPTCHA but was not independently
measured against Entel.

A token is consumed when Entel submits it to Google's verification service. A
request rejected earlier, such as a CSRF 403, does not appear to consume it.

The following browser message is produced by Entel's own duplicate-render
behavior and did not prevent operation:

```text
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

1. GET the application root.
2. GET `moduleversioninfo`.
3. Send the first POST.
4. Read the 403 response and its `Set-Cookie` headers.
5. Extract `crf` from `nr2Users`.
6. Resend with the `X-CSRFToken` header.

`nr2Users` is URL-encoded:

- `%3d` represents `=`
- `%3b` represents `;`

Only `nr2Users` is available through `document.cookie`.

The following cookies are HttpOnly and require CDP access:

- `osVisit`
- `osVisitor`
- `nr1Users`

## Acceptance differences

A normal, previously used Chrome profile accepted documents that a fresh
automated browser rejected.

Observed on 2026-07-17:

- manually driven everyday Chrome: 6 of 6, then 5 of 5
- same Chrome with the Step 2 block removed: 0 of 5
- fresh automated browsers using the same request flow: 0 of 5 across the tested
  profiles and network exits

This suggests that acceptance depends on browser-level signals beyond the
visible request body. The exact signal remains unidentified.

Whether a person or script initiates the request is not sufficient by itself to
explain the difference. Input method also affects results, as described below.

`packages/capture` exists to run the flow through an established browser
profile.

## Ruled-out variables

The following were tested without changing acceptance:

- TLS client choice: `httpx`, `curl_cffi`, and the in-page path behaved the
  same.
- `navigator.webdriver` and common stealth properties through
  `playwright-stealth`.
- Windows Chrome User-Agent and UA-CH values.
- Intel WebGL vendor and renderer values.
- `deviceMemory` and `languages`.
- Hardware GPU availability. SwiftShader was sufficient for successful runs.
- The `_GRECAPTCHA` cookie. Copying it from a working browser did not transfer
  acceptance.
- Exit IP. Two Peru residential exits failed, while a working browser shared the
  probe host's public IP.
- Cookie clearing and profile reuse.
- Token length, uniqueness, and mint time.
- Human versus programmatic form interaction within the same browser.

Measured rejected tokens:

- length: 1316 to 1358 characters
- mint time: 164 to 255 ms
- distinct token prefix on each request

These tokens were fresh and unspent, but Entel still rejected them.

A disposable reCAPTCHA test key was not useful for diagnosis. It returned the
same coarse score for every tested configuration and did not reflect Entel's
trained model.

## CDP input versus OS input

A controlled comparison used the same browser, site, form, and IP.

```text
CDP input with `isTrusted: true`  -> HasErrorDebt: true
PyAutoGUI input through X11       -> accepted, 3 of 3
```

A CDP-generated click reports `isTrusted: true`, but that alone does not make it
equivalent to OS-level input.

The OutSystems input mask also treated the input methods differently:

- CDP `send_keys` entered only the first character.
- PyAutoGUI entered the complete value.

## Replaying through a plain HTTP client

A token minted in the browser could not be moved successfully to `httpx`.

Controlled sequence:

1. Block the Step 2 XHR.
2. Capture the application's exact request body.
3. Preserve the fresh, unspent token.
4. Export the complete browser cookie jar through CDP.
5. Replay the request through `httpx`.

The replay returned:

```text
HasErrorDebt: true
```

A normal in-browser request in the same session returned a real debt amount
seconds later.

The session and IP were therefore still valid.

The flow "mint in browser, request through `httpx`" does not work without
reproducing some additional browser-bound property. Whether that property is
TLS-related remains unresolved.

This limits throughput and prevents the current Entel implementation from moving
into `packages/fetch`.

## Display and input environment

Xvfb is sufficient.

A PyAutoGUI-driven form running on a headless virtual display returned real
debt. XTEST input through Xvfb was therefore accepted in the tested environment.

PyAutoGUI sends input to whichever window currently has OS focus. Sharing a
display between sessions can silently corrupt runs.

Observed failures included:

- an input containing only `2`
- an input receiving no text
- another window receiving the keystrokes

Each worker should use its own display.

PyAutoGUI uses screen coordinates. Elements below the viewport should first be
centered:

```js
element.scrollIntoView({ block: "center" });
```

On a 1080p display, dropdown options rendered near `y=1157` before scrolling.

## WSL

SeleniumBase's `xvfb=True` does not work under the tested WSLg environment.

WSLg mounts `/tmp/.X11-unix` read-only without the sticky bit. A new X server
cannot create a Unix socket there, and `chmod` cannot change the directory.

Start Xvfb manually:

```sh
Xvfb :99 -screen 0 1920x1080x24 -listen tcp -nolisten unix
export DISPLAY=127.0.0.1:99
```

Then run SeleniumBase with `headed=True`.

`-nolisten unix` prevents Xvfb from accessing `/tmp/.X11-unix`.

`XAUTHORITY` must point to an existing file, but the file may be empty.

## SeleniumBase

On a normal Linux server, this works:

```python
SB(uc=True, xvfb=True)
```

One known issue affects the display size.

`activate_cdp_mode()` creates a second virtual display without forwarding
`xvfb_metrics`. As a result:

```python
xvfb_metrics="1920,1080"
```

may still produce a 1366x768 display. PyAutoGUI then fails when asked to click
outside that smaller screen.

This behavior is documented in SeleniumBase discussion
[#3664](https://github.com/seleniumbase/SeleniumBase/discussions/3664).

UC mode also removes the proxy authentication extension. An authenticated
upstream proxy therefore produces blank pages.

`browser/local_proxy.py` handles this by running a local unauthenticated relay:

```text
Chrome -> 127.0.0.1 relay -> authenticated upstream proxy
```

The relay adds the upstream credentials itself.
