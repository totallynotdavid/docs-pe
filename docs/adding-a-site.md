# Adding a site

Sites move through three independent packages:

```text
capture -> browser -> fetch
 discover   automate   scale
```

The workflow can stop at any stage when the site does not need the next one.
Entel is one example. See
[the site notes](sites/entel.md#replaying-through-plain-http).

## 1. Capture the wire behavior

Add the site definition under `packages/capture/capture/sites/<name>/`. The
capture package owns the page object and capture script for that site.

```python
@dataclass(frozen=True)
class Entel(Site):
    name = "entel"
    origin = "https://miperfil.entel.pe"
```

Run capture with your own Chrome profile:

```sh
uv run capture --input docs.csv --output debts.csv --site entel
```

Open the generated capture script in DevTools, perform one real lookup, and run
the captured clients. Capture proves that the request works with a human browser
before automation adds another source of failure.

## 2. Choose the next package

| Requirement                                                 | Package   |
| ----------------------------------------------------------- | --------- |
| JavaScript, reCAPTCHA, Cloudflare, or a browser fingerprint | `browser` |
| Plain HTTP is sufficient                                    | `fetch`   |

Use the package README for its local conventions. See
[Architecture](../ARCHITECTURE.md#package-boundaries) for dependency rules.

## 3. Implement and register it

Each package has its own site module and parser. The filenames are package
contracts, not a shared template:

- `capture` typically uses `page.py` and capture helpers.
- `browser` uses a browser site class and parser.
- `fetch` uses `site.py`, request modules, and `parser.py` where needed.

Register the site in that package's `sites/registry.py`. Keep retry policy in
`fetch/fetch/domain/policy.py`; a site reports a fault but does not decide
whether the run retries it.

## 4. Record site behavior

Put gates, wire behavior, error codes, and failure modes in
`docs/sites/<name>.md`. Put dated measurements in `docs/reports/`. Package
README files should link to those references instead of repeating them.
