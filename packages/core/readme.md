# core

`core` executes one lookup through a site and proxy provider. It owns site
contracts, provider schemas, sessions, parsing, fault classification, retries,
and circuit breakers.

`cli` uses core for standalone batch jobs. `portal` uses core for leased worker
items. Core has no command parsing, environment loading, SQLite state, CSV
exports, or portal imports.

Add HTTP sites under `core/sites/` and proxy providers under `core/proxy/`.
Keep retry decisions in `core/domain/policy.py`.
