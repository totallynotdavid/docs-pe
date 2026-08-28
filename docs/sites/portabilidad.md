# Portabilidad

The number-portability lookup is available at
`https://consulta.portabilidad.pe/` and is implemented in
[browser](../../packages/browser/readme.md). It accepts a nine-digit mobile
number and returns the current carrier and portability history.

## Output

The exported columns are:

```text
subject, receptor, cedente, asignatario_original,
fecha_ventana, estado, current_carrier, observed_at
```

`cedente` and `fecha_ventana` may be absent in the site's result for some
numbers. The parser preserves those fields as empty strings. The other result
fields must be present for a lookup to be accepted.

## Browser protocol

The page must render `#hf-number` and the submit control before the lookup
starts. The client loads a fresh page, enters the number, waits for the form to
settle, and submits after Turnstile has minted a token.

The Turnstile checkbox is inside a closed shadow root, so the token step uses a
GUI click. The token is then read from `window.turnstile.getResponse()`. If the
token is not minted, or the page reports a CAPTCHA error, the browser package
classifies the attempt as a browser rejection and applies its configured
retry policy.

This site has no warm-up identifier. A missing result marker after submission
is a browser failure.
