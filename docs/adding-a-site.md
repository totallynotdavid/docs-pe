# Adding a site

Add a site in the execution mode that can satisfy its protocol. The packages
are independent, so discovery does not obligate the project to ship browser or
HTTP automation.

Capture, browser, and HTTP implementations are separate packages. Capture can
provide protocol knowledge for a browser or HTTP implementation, but the
packages do not import one another.

## 1. Discover the request

If the request is not understood, define the site under
`packages/capture/capture/sites/<name>/` and register it in that package's
registry. Run capture with your own Chrome profile:

```sh
uv run capture \
  --input rucs.csv \
  --output results/<name>-capture.csv \
  --site <name>
```

Complete one lookup manually and record the request, response, cookies, tokens,
and browser actions that are part of the protocol. A request copied from
DevTools is not necessarily sufficient outside the browser.

## 2. Choose an execution mode

| Requirement                                             | Package   |
| ------------------------------------------------------- | --------- |
| A human browser profile is part of the working protocol | `capture` |
| Chrome, JavaScript, or a browser gate is required       | `browser` |
| The request works with an ordinary HTTP client          | `core`    |

Read the package `readme.md` before implementing the site. Keep adapters and
parsers local. Do not import site code between `capture`, `browser`, and `core`.

## 3. Implement and register

Register the site in the registry for the package that owns it. Put HTTP
request and parsing code under `packages/core/core/sites/<name>/`, browser page
behavior under `packages/browser/browser/sites/<name>/`, and capture code under
`packages/capture/capture/sites/<name>/`. Keep retry decisions in
`core.domain.policy`.

Add focused tests for accepted input, response parsing, empty results, and the
failure modes that must not be mistaken for success. Run the package checks
through `mise` or `uv` as described in [Contributing](../CONTRIBUTING.md).

## 4. Document the contract

Create or update `docs/sites/<name>.md` with the current endpoint, input and
output fields, gates, and failure semantics. Put dated measurements in
`docs/reports/`. Link to the site note from the package `readme.md` instead of
copying its protocol.

Before opening a change, verify the command, registry entry, tests, and links
from the repository root. A site is complete when a maintainer can reproduce a
small run and can tell a valid empty result from a failed lookup.
