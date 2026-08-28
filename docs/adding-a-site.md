# Adding a site

Add a site in the smallest execution mode that can satisfy its protocol. The
packages are independent, so discovery does not obligate the project to ship
browser or HTTP automation.

```text
capture -> browser -> core
```

## 1. Discover the request

Create the site definition under
`packages/capture/capture/sites/<name>/` and register it in that package's
registry. Run capture with your own Chrome profile:

```sh
uv run capture \
  --input rucs.csv \
  --output results/<name>-capture.csv \
  --site <name>
```

Complete one lookup manually. Keep the request, response, required cookies,
tokens, and browser actions that are part of the working protocol. Do not
assume that a request copied from DevTools is sufficient outside the browser.

## 2. Choose an execution mode

| Requirement | Package |
| --- | --- |
| A human browser profile is part of the working protocol | `capture` |
| Chrome, JavaScript, or a browser gate is required | `browser` |
| The request works with an ordinary HTTP client | `core` |

Read the package README before implementing the site. Keep each package's
adapter and parser local. Do not import site code between `capture`, `browser`,
and `core`.

## 3. Implement and register

Register the site in the registry for the package that owns it. For `core`,
put request and parsing code under
`packages/core/core/sites/<name>/` and keep retry decisions in
`core.domain.policy`. For `browser`, put session and page behavior under
`packages/browser/browser/sites/<name>/`. For `capture`, keep the relay and
browser script under `packages/capture/capture/sites/<name>/`.

Add focused tests for accepted input, response parsing, empty results, and the
failure modes that must not be mistaken for success. Run the package checks
through `mise` or `uv` as described in [Contributing](../CONTRIBUTING.md).

## 4. Document the contract

Create or update `docs/sites/<name>.md` with the current endpoint, input and
output fields, gates, and failure semantics. Put dated measurements and
investigation results in `docs/reports/`. The package README should link to
the site note rather than copy its protocol.

Before opening a change, verify the command, registry entry, tests, and links
from the repository root. A site is complete when a maintainer can reproduce a
small run and can tell a valid empty result from a failed lookup.
