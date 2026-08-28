# Entel

Entel's debt lookup is available at `miperfil.entel.pe`. The working
implementations are in [browser](../../packages/browser/readme.md) and
[capture](../../packages/capture/readme.md). It is not implemented in
[fetch](../../packages/fetch/readme.md): the accepted request depends on
browser state that a plain HTTP client does not reproduce.

## Result contract

The debt request is a POST to the OutSystems `DataActionGetData` endpoint:

```text
/screenservices/PE_Web_Cobro_Online_CW/OnlinePayment/
OnlinePayment_Step2/DataActionGetData
```

The request body is captured from the live application. The fields that the
automation changes are:

| Field | Requirement |
| --- | --- |
| `screenData.variables.DocumentType` | `DNI` or `RUC`. |
| `screenData.variables.DocumentNumber` | The identifier being queried. |
| `screenData.variables.RecaptchaId` | The string `"0"`. |
| `clientVariables.TokenCaptchaV3` | A fresh token minted in the page. |
| `viewName` | `OnlinePaymentFlow.OnlinePayment`. |
| `versionInfo.moduleVersion` | The current value from `moduleversioninfo`. |

Keep the complete captured body. Constructing a smaller body or copying a
token into an HTTP client omits browser state that Entel validates.

A valid no-debt response has `HasErrorDebt: false` and `DebtTotal: "0.0"`.
The rejection response has HTTP 200, `HasErrorDebt: true`, `DebtTotal: "0.0"`,
an empty account list, and an empty `DocumentNumber`. Use `HasErrorDebt` to
distinguish a valid zero-debt result from a rejected lookup.

## Browser sequence

The application uses `XMLHttpRequest`, not `window.fetch`, for the debt call.
The browser implementation therefore:

1. captures the Step 2 request template from `XMLHttpRequest.prototype.send`;
2. blocks that request so the application remains on the input step;
3. obtains a fresh reCAPTCHA v3 token in the page; and
4. sends the captured template with the current document fields and token.

The token must be minted while the page is still on Step 1. A token minted
after the application advances to Step 2 is rejected. The request that
initializes the widget must remain enabled; otherwise `RecaptchaId` is empty.

## CSRF

The application may first answer with HTTP 403 and
`{"exception":{"message":"Invalid Login"}}`. This response means that the
anti-forgery token is missing, not that reCAPTCHA failed.

The bootstrap path is:

1. load the application root and module version;
2. send the first request;
3. read the `Set-Cookie` headers from the 403 response;
4. extract `crf` from the URL-encoded `nr2Users` cookie; and
5. resend the request with `X-CSRFToken`.

The other required cookies are HttpOnly and must be read through the browser
debugging protocol. `document.cookie` exposes only `nr2Users`.

## Rejections and operations

Entel does not expose the reCAPTCHA score or a rejection reason. Invalid,
reused, expired, or missing tokens and stale sessions can produce the same
response. The browser package treats that response as `RejectedError`, mints a
new token, and applies its configured retry and session-restart limits.

A hard browser error stops the run. It indicates that the page or session could
not complete the protocol.

If a fresh browser still receives rejections, inspect the page sequence, the
CSRF cookie, and the provider exit before increasing retry counts. This site
has accepted an established interactive Chrome profile while rejecting an
automated profile with the same visible request. That is the boundary that
keeps Entel in `browser` and `capture`.
