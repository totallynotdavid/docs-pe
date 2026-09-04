# capture

`capture` discovers a site's request and response through a real Chrome profile.
It is useful before automation and can remain the correct execution mode when
the site accepts an established browser but rejects a new one.

For example, capture an Entel lookup with a RUC input:

```sh
uv run capture \
  --input rucs.csv \
  --output results/entel-capture.csv \
  --site entel
```

Capture does not use a proxy. The browser profile supplies the site's cookies,
reputation, and interactive state. Read
[the Entel note](../../docs/sites/entel.md) for the reason this distinction
matters.

## Workflow

1. Define the site under `packages/capture/capture/sites/<name>/`.
2. Run `capture` to generate the browser script and local relay state.
3. Open the target site in your own Chrome profile.
4. Paste the generated script into DevTools and complete one lookup manually.
5. Let the captured clients replay the request and post responses to the relay.
6. Use the captured request and response to decide whether the site belongs in
   `browser` or `fetch`.

The generated script is written beside the output as
`<output>.<site>-capture.js`. The default state path is
`<output>.state.sqlite3`. Add `--diagnostics <path>.jsonl` for redacted request
and browser timing data.

Capture owns discovery code only. `browser` and `fetch` have independent site
implementations, so moving a site between packages means carrying over the
protocol knowledge and validating it again. See
[Adding a site](../../docs/adding-a-site.md).

For the complete option list:

```sh
uv run capture --help
```
