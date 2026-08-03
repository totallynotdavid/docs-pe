# Entel debt lookup: how the site works

Field record for `miperfil.entel.pe/PE_Web_Cobro_Online_EU/` ("Paga tu deuda"),
investigated 2026-07-17. Both
[`packages/browser`](../packages/browser/readme.md) and
[`packages/capture`](../packages/capture/readme.md) drive this site, and each
holds its own copy of the recipe. This file is the shared understanding behind
both.

The site is an OutSystems Reactive app. Debt comes from one server action gated
by reCAPTCHA v3.

## Every rejection looks identical

A rejected lookup returns HTTP 200 with `HasErrorDebt: true`,
`DebtTotal: "0.0"`, an empty account list, and a blank `DocumentNumber`. That
one shape covers a bad token, a spent token, an expired token, an empty
`RecaptchaId`, and a dead session. There is no error code and no score anywhere
in the response.

Almost every difficulty with this site follows from that fact: a single reply
cannot tell you which thing went wrong, so every question has to be answered by
a controlled experiment.

`HasErrorDebt: false` with `DebtTotal: "0.0"` is genuinely no debt. The flag
decides, never the amount.

## The mechanism: mint on Step 1

The app sends its debt request over `XMLHttpRequest`, not `fetch`, so a
`window.fetch` hook intercepts nothing. Filling the form normally advances from
the document screen (Step 1) to a payment screen (Step 2) and fires the request
during that transition.

```
Token minted while the page is still on Step 1   ->  ACCEPTED
Token minted after the app reaches Step 2        ->  REJECTED
```

So the whole trick is to hold the page on Step 1. Hooking
`XMLHttpRequest.prototype.send`, capturing the `Step2/DataActionGetData` body as
a template, and returning without forwarding it does exactly that. The debt call
never leaves the browser and the loading spinner hangs forever. **That hang is
the success state.** Driving the form once this way only captures the template,
so the document used to drive it does not matter.

With the template captured, each lookup is one fresh token and one POST from
inside the page: roughly 0.8 to 1.4 s, no page reload, no decay across lookups,
no cleanup between them.

Removing only the block, in the same browser minutes later, yields 0 of 5. That
is the strongest single-variable result on record here.

## Endpoint contract

```
POST .../screenservices/PE_Web_Cobro_Online_CW/OnlinePayment/OnlinePayment_Step2/DataActionGetData
Content-Type: application/json; charset=UTF-8
```

Request fields that matter:

| Field                                 | Value                                               |
| ------------------------------------- | --------------------------------------------------- |
| `screenData.variables.DocumentType`   | `"RUC"` or `"DNI"`, read as a plain string          |
| `screenData.variables.DocumentNumber` | the document                                        |
| `screenData.variables.RecaptchaId`    | `"0"`, required and validated server-side           |
| `clientVariables.TokenCaptchaV3`      | the fresh token                                     |
| `viewName`                            | `"OnlinePaymentFlow.OnlinePayment"`                 |
| `versionInfo.moduleVersion`           | rotates; `GET .../moduleservices/moduleversioninfo` |

Because `DocumentType` is read as a plain string, a template captured with any
one kind serves both: override the field per lookup instead of re-driving the
dropdown.

`RecaptchaId` must be the string `"0"`, which is also the reCAPTCHA widget id.
Blocking `OnlinePayment_WB/DataActionGetData` makes the app post
`RecaptchaId: ""`, and the lookup then fails even in a real browser with a real
app-minted token: that call is what sets up the widget. A hand-built body fails
for the same reason, so capture the template from a live request.

The large `PaymeForm` block in the body appears inert.

### reCAPTCHA

- Site key `6LdUZwUcAAAAAC_K3DlqC_WHKbDwXfYXZrV0Xrx5`, action `"SearchDebt"`,
  confirmed live by hooking `grecaptcha.execute`.
- The widget id is the string `"0"`, not the number, and it matches
  `RecaptchaId`.
- Tokens run about 1300 characters and expire near 120 s. Single use is assumed
  from general reCAPTCHA behaviour and has not been measured against Entel.
- A token is consumed when Entel calls `siteverify`. A request rejected earlier
  in the pipeline, such as a CSRF 403, does not consume it.
- `"reCAPTCHA has already been rendered in this element"` is Entel's own
  double-render bug. Harmless.

### CSRF

HTTP 403 with `{"exception":{"message":"Invalid Login"}}` is a missing
anti-forgery token, not a captcha problem. Bootstrap is: GET the app root, GET
`moduleversioninfo`, POST once and take the 403 plus its `Set-Cookie`, read
`crf` out of `nr2Users`, then resend with an `X-CSRFToken` header. `nr2Users` is
URL-encoded, with `%3d` for `=` and `%3b` for `;`. Only `nr2Users` is readable
from `document.cookie`; `osVisit`, `osVisitor`, and `nr1Users` are HttpOnly and
need CDP.

## What decides acceptance: browser reputation

A real everyday Chrome profile clears the borderline documents that a cold
automated browser cannot. Hand-driven real Chrome returned 6 of 6 and then 5 of
5 on 2026-07-17, and 0 of 5 with the block removed in the same browser and hour.
A cold automated browser running the identical recipe scored 0 of 5 across every
profile and exit tried.

Whether a person or a script clicks makes no difference. What matters is the
browser Google has observed over time. This is why `packages/capture` exists.

### Ruled out

Each was tested directly and changed nothing about acceptance:

- **TLS fingerprint.** `httpx` and `curl_cffi` behaved the same as the in-page
  path.
- **`navigator.webdriver` and the usual stealth surface**, via
  playwright-stealth.
- **User-Agent and UA-CH** brand strings spoofed to Windows Chrome.
- **WebGL vendor and renderer** spoofed to Intel, plus `deviceMemory` and
  `languages`.
- **A real GPU.** None exists under WSL anyway; SwiftShader is sufficient.
- **The `_GRECAPTCHA` cookie.** Injecting a real high-reputation browser's
  cookie survived on every domain yet scored 0 of 6, while that real Chrome
  passed the same documents in the same minute. Reputation is bound server-side
  to the browser Google observed. It does not travel in a copied cookie value.
- **Exit IP.** Two fresh clean Peru residential exits failed identically, and
  the working Chrome shared the probe host's public IP.
- **Cookie and profile state.** `clear_cookies` plus reload changed nothing, a
  reused `user_data_dir` failed twice, and UC mode issues a fresh profile per
  run regardless.
- **Token quality.** With the block verified in place, tokens measured 1316 to
  1358 characters with a distinct head per call and a 164 to 255 ms mint. Entel
  accepts none of them. The rejection is server-side on a valid, fresh, unspent
  token.
- **Human against programmatic input** in the same browser: the same
  intermittent rate.

The disposable-key reCAPTCHA harness is not diagnostic here. An untrained
free-tier key returns coarse buckets and reported 0.9 for every configuration,
so it cannot see Entel's trained model.

### CDP input is distinguishable, and `isTrusted` is not sufficient

Same browser, same site, same form, same IP, only the input path differing:

```
CDP-driven (sb.cdp.click / send_keys, isTrusted: true)  ->  HasErrorDebt: true
OS-level (PyAutoGUI through a real X display)           ->  real debt, 3/3
```

A CDP click reports `isTrusted: true` and is rejected anyway. Independently, the
OutSystems input mask also drops CDP `send_keys`, landing only the first
character, while accepting PyAutoGUI keystrokes cleanly.

### Still unresolved

What differs between the passing and failing case is unidentified. Every input
to the request that can be controlled from this side has been matched to the
passing run.

The same automated browser passed 3 of 3 and 4 of 4 one morning and 0 of 4 that
afternoon, so it degraded during the day rather than being rejected by build.
Those cliffs followed bursts of rapidly minted rejected tokens, and those bursts
came from probes missing the Step 2 block, which were minting tokens that could
never pass. **Whether a correct blocking loop degrades at all is unmeasured.** A
browser-level reputation surviving profile wipes, IP changes, and cookie clears
would fit the observations, but so would other explanations, and nothing
separates them yet.

### The token does not survive being moved to a plain HTTP client

One clean controlled observation: block the app's Step 2 XHR so the token is
never spent at `siteverify`, capture the app's exact body plus that unspent
token, and replay from `httpx` with the full cookie jar taken through CDP. The
replay returned `HasErrorDebt: true` while a normal in-browser lookup in the
same session seconds later returned a real amount. Session and IP were healthy.

So "mint in a browser, call from `httpx`" does not work as-is. Something binds
the token to the browser context, and whether that something is TLS is
unresolved. This is what caps throughput and what keeps Entel out of
`packages/fetch`.

## Environment

Xvfb is sufficient. A PyAutoGUI-driven form on a headless virtual display
returns real debt, so XTEST input through Xvfb is not distinguishable by
reCAPTCHA. That is what makes per-worker displays and horizontal fanout
possible.

PyAutoGUI types into whatever window holds OS focus, so a shared display
corrupts runs silently. One drive left an input holding `2` instead of a
document, and another typed nothing at all, because a human was typing
elsewhere. Give each session its own display.

It also clicks screen coordinates, so anything below the fold needs
`scrollIntoView({block: "center"})` first. The dropdown options render near
y=1157 on a 1080p display.

On WSL, SeleniumBase's `xvfb=True` cannot work: WSLg mounts `/tmp/.X11-unix`
read-only without the sticky bit, so no X server can bind a socket there and
`chmod` cannot change it. Start the display manually instead, and run
SeleniumBase with `headed=True`:

```sh
Xvfb :99 -screen 0 1920x1080x24 -listen tcp -nolisten unix
export DISPLAY=127.0.0.1:99
```

`-nolisten unix` is the trick: the server never touches `/tmp/.X11-unix`.
`XAUTHORITY` must point at a file that exists, but it can be an empty one.

On a normal server `SB(uc=True, xvfb=True)` works, with one known issue:
`activate_cdp_mode()` spawns a second virtual display and does not pass
`xvfb_metrics` through, so `xvfb_metrics="1920,1080"` silently yields 1366x768
and PyAutoGUI then raises "cannot click on point ... outside screen"
(SeleniumBase discussion #3664).

UC mode strips the proxy-auth extension, so an authenticated upstream proxy
yields blank pages. `browser/local_proxy.py` terminates the auth locally: Chrome
talks to an unauthenticated `127.0.0.1` relay that attaches the upstream
credentials itself.
