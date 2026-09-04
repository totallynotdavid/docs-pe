# core

`core` turns one document and site into a lookup result. It owns the site and
proxy-provider contracts, HTTP sessions, parsing, fault classification, retry
policy, and circuit breakers.

The standalone CLI and portal workers call core. Core does not own command-line
parsing, environment loading, SQLite state, CSV exports, or portal storage.

## Extension points

- Add HTTP sites under `core/sites/<name>/` and register them in
  `core/sites/registry.py`.
- Add proxy providers under `core/proxy/<name>.py` and register their schemas in
  `core/proxy/registry.py`.
- Keep fault-to-retry decisions in `core/domain/policy.py`. Site adapters report
  facts; they do not choose retry actions.

Read [Architecture](../../ARCHITECTURE.md) for lifecycle contracts and
[Adding a site](../../docs/adding-a-site.md) for the implementation workflow.
