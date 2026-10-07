# Adding a site

A site belongs in the package that can satisfy its protocol: `capture`,
`browser`, or `core`. These three packages import none of one another
([Architecture](architecture.md) has the boundaries). Capture can supply
protocol knowledge to a browser or HTTP implementation, which then carries it
over and validates it again.

## 1. Discover the request

If the request is not understood, define the site under
`packages/capture/capture/sites/<name>/` and register it in
[`sites/registry.py`](../packages/capture/capture/sites/registry.py). Run
capture with your own Chrome profile:

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

Read the package readme before implementing the site. Keep adapters and parsers
local to the package.

## 3. Implement and register

Register the site in the registry of the package that owns it:

| Package   | Site code                                | Registry                                                             |
| --------- | ---------------------------------------- | -------------------------------------------------------------------- |
| `core`    | `packages/core/core/sites/<name>/`       | [`sites/registry.py`](../packages/core/core/sites/registry.py)       |
| `browser` | `packages/browser/browser/sites/<name>/` | [`sites/registry.py`](../packages/browser/browser/sites/registry.py) |
| `capture` | `packages/capture/capture/sites/<name>/` | [`sites/registry.py`](../packages/capture/capture/sites/registry.py) |

A core site reports facts such as a ban signal or a malformed response. Retry
decisions stay in
[`core/domain/policy.py`](../packages/core/core/domain/policy.py).

Add tests for accepted input, response parsing, empty results, and the failure
modes that must not be mistaken for success. Run them as
[Contributing](../.github/CONTRIBUTING.md) describes.

## 4. Document the contract

Create or update `docs/<name>.md` with the current endpoint, input and output
fields, gates, and failure semantics, and add it to the [index](readme.md). Link
to it from the package readme instead of copying its protocol.

The site is complete when a maintainer can reproduce a small run and tell a
valid empty result from a failed lookup.
