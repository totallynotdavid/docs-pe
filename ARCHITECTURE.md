# Architecture

The repository turns identifiers and telephone numbers into site-specific
results. It has one shared lookup engine and four execution surfaces:

```text
capture       request discovery in a human-operated Chrome profile
browser       site lookups through Chrome and CDP
fetch         unattended site lookups through HTTP and proxy providers
portal        web submission and a worker fleet

fetch  ─┐
portal ─┴─> core
```

`capture`, `browser`, and `core` are independent packages. The arrow in the
diagram is an import boundary, not a workflow requirement. Capture can inform a
browser or HTTP implementation, but those packages do not import capture code.
The standalone executable is named `fetch` and lives in `packages/cli`.

## Execution modes

`capture` uses a human-operated Chrome profile to discover a request and its
response. It is useful when browser reputation or a visual gate is part of the
site behavior.

`browser` drives Chrome through CDP. It owns browser sessions, browser gates,
and site behavior that cannot be reduced to an HTTP request.

`core` runs site requests through proxy providers. It owns provider sessions,
site parsers, fault classification, and retry policy. The [site registry](packages/core/core/sites/registry.py)
and [provider registry](packages/core/core/proxy/registry.py) are its extension
points.

`cli` is the standalone `fetch` tool. It owns command parsing, environment
loading, local SQLite state, CSV exports, and sharded-job tools.

The packages intentionally copy site knowledge between modes instead of sharing
implementation code. A site can therefore remain in `capture` while its protocol
is investigated, or remain in `browser` when plain HTTP is not a valid
implementation. See [Adding a site](docs/adding-a-site.md) for that workflow.

The portal has three service processes and one administrative interface. `web`
serves the browser application, `worker-api` owns credential decryption and
result publication, and `worker` claims queue work directly through a scoped
PostgreSQL role. `portal-admin` owns migrations, provisioning, worker identity,
and key management. Service processes do not run administrative commands during
startup.

## Standalone fetch lifecycle

```text
input CSV
  -> normalize and plan document/site pairs
  -> skip pairs with terminal outcomes
  -> run site lanes with sessions and readiness checks
  -> parse and classify each response
  -> write the outcome to SQLite
  -> export CSV projections
```

A lane owns the session, readiness check, proxy rotation, and lookup execution
for one site. A site reports facts such as a ban signal or a malformed response.
`core.domain.policy` decides what those facts mean for retry and breaker
accounting. Site modules do not implement their own retry policy.

## Outcome state

The SQLite database is the source of truth for a standalone run. Each
document/site pair has one durable row when it has produced an outcome.

| Status      | Meaning                                                                                             |
| ----------- | --------------------------------------------------------------------------------------------------- |
| `ok`        | The site returned a valid result. An empty result is valid only for a site that allows it.          |
| `not_found` | The site explicitly confirmed that the document is absent.                                          |
| `failed`    | The latest attempt failed. The row remains retryable until its cumulative attempt limit is reached. |

`MAX_ATTEMPTS` limits attempts in one process. `MAX_TOTAL_ATTEMPTS` limits the
same pair across relaunches. A circuit-breaker wait is not a healthy contact and
does not consume the document's attempt budget.

CSV files contain projections of successful rows, not a complete progress
ledger. In particular, a successful lookup that returns no rows has an `ok`
outcome but contributes no data row to a result CSV. Query SQLite when logs and
CSV files disagree.

Standalone breaker state is stored by site and provider in the outcome database
and restored when the same output path resumes. The portal stores its fleet
breaker in PostgreSQL so all workers draw work from the same open or closed
state.

## Portal lifecycle

The portal creates one queue item per accepted document/site pair. A worker
self-enrolls through `worker-api`, claims queue and proxy-slot state through its
scoped PostgreSQL role, executes a core lookup in its own process, and sends
credential reveals and results to `worker-api`. Claim leases and lease fences
prevent a late worker from publishing after cancellation or reassignment.

Each publish carries the complete fetch-attempt history. PostgreSQL records one
row per attempt in `portal_lookup_attempts`, including attempts from a stale
publish fence, in the same transaction as the terminal item and entry outcome.
The terminal tables describe the final result; the attempt table describes the
cost and failure history that led to it.

PostgreSQL owns queue leases, cancellation fences, reusable entries, team
access, worker identities, fleet proxy-slot leases, and the fleet circuit
breaker. Uploaded inputs and result payloads live in the configured object
store. The worker role is limited to queue, lease, heartbeat, slot, and result
metadata operations. Stored credential ciphertext remains accessible only to
`worker-api`, which holds the master key.
