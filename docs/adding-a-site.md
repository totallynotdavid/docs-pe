# Adding a site

A site graduates through up to three packages, trading investigation effort for
throughput:

```
capture  →  browser  →  fetch
(learn)     (automate)   (scale)
```

You don't have to reach `fetch`. Entel, for instance, is stuck at `browser`
permanently: see [sites/entel.md](sites/entel.md#replaying-through-plain-http)
for why. A site only moves forward once you've confirmed the next stage actually
works; there's no obligation to chase throughput a site doesn't need.

## Step 1: Discover the request, with capture

Add a site definition to `packages/capture/capture/sites/<name>/page.py`:

```python
@dataclass(frozen=True)
class Entel(Site):
    name = "entel"
    origin = "https://miperfil.entel.pe"
```

Run it:

```sh
uv run capture --input docs.csv --output debts.csv --site entel
```

This generates `debts.entel-capture.js`. Open the site in your own Chrome, paste
the script into DevTools, complete one lookup manually so the script captures
the request template, then click `RUN CLIENTS`. See
[packages/capture/readme.md](../packages/capture/readme.md) for the mechanics.

Capture uses your real, logged-in-for-months Chrome profile: no proxy config, no
automation to detect. That's the point: it tells you the request shape works at
all, before you spend effort automating it. If a site's gate (reCAPTCHA,
Cloudflare) scores automated browsers lower regardless of request correctness
(as Entel's does, see [sites/entel.md](sites/entel.md#why-automation-fails)),
capture may also end up being the _permanent_ home for that site, not just the
first stop.

## Step 2: Decide where it goes next

| Site needs...                                                   | Implement in                             |
| --------------------------------------------------------------- | ---------------------------------------- |
| JavaScript, reCAPTCHA, Cloudflare, or a real Chrome fingerprint | [browser](../packages/browser/readme.md) |
| Nothing beyond plain HTTP                                       | [fetch](../packages/fetch/readme.md)     |

Most sites need `browser` first even if they'll eventually reach `fetch`: you
don't know a site is pure-HTTP until you've watched its network traffic in
capture.

## Step 3: Implement it

Each package requires the same two things, independently:

1. `sites/<name>/page.py`: drive the site (or, in fetch, build the request) and
   capture responses
2. `sites/<name>/parse.py`: convert the response into a result row
3. One entry in that package's `sites/registry.py`

No shared code between packages: see [architecture.md](architecture.md) ("do not
add cross-package imports") for why. Concretely: `browser/sites/entel/parse.py`
and a hypothetical `fetch/sites/entel/parse.py` would each maintain their own
parser, even though they're parsing the same site. This is deliberate: it lets a
site be reliable in `capture` while still broken in `browser`, or reliable in
`browser` while `fetch` doesn't exist for it yet, without one package's fix
touching another's.

## Step 4: Write down what you learned about the site

Site behavior (gate type, wire protocol, error codes, failure rates) goes in
`docs/sites/<name>.md`, not in the package readme that happens to implement it
first. The fact that Entel's reCAPTCHA score depends on browser reputation is
true regardless of whether you're reading it from `browser` or `capture`: put it
once, in [sites/entel.md](sites/entel.md), and link to it from both. See
[sites/](sites/) for what's already known about each implemented site.
