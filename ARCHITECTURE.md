# Architecture

This repository turns rows of Peruvian identity documents into site-specific
results. It has three lookup modes:

- `capture` discovers a site's request and response with a human-operated Chrome
  profile.
- `browser` automates sites that need Chrome, JavaScript, or a browser gate.
- `fetch` runs the discovered lookup at scale through proxy providers.

The portal submits jobs to the same fetch pipeline and adds authentication,
queueing, and worker management around it.

## Package boundaries

Each package owns the site parsers, columns, and document classes for its mode
of operation.

```text
capture -> browser -> fetch
portal  -> fetch
```

The arrows describe the workflow and the allowed dependency direction. Do not
import between `capture`, `browser`, and `fetch`. `portal` may import `fetch` as
a library.

## Fetch lifecycle

```text
input CSV
  -> plan accepted document/site pairs and skip completed outcomes
  -> run one lane per site with its own session and readiness checks
  -> lookup, parse, and classify the response
  -> write the outcome to SQLite
  -> project finished outcomes to CSV
```

A lane owns the session, readiness check, lookup, and proxy rotation for one
site. Sites report domain facts. `fetch.domain.policy` maps those facts to retry
actions so circuit-breaker accounting stays centralized.

## Outcome state

The SQLite outcome store is the source of truth for a run. The durable states
are:

- `ok`: the site returned a valid result. An empty result is valid only for a
  site that explicitly allows it.
- `not_found`: the site confirmed that the document is absent.
- `failed`: the latest attempt failed and remains eligible for retry until its
  attempt limit is reached.

There are no pending rows. A pair that has not produced an outcome has not been
written yet. CSV files are projections written at the end of a run and may not
exist while work is in progress. Query SQLite outcomes when logs and output
files disagree.

Healthy contacts consume the lookup budget. Circuit-breaker blocked contacts do
not. `MAX_ATTEMPTS` limits one lookup and `MAX_TOTAL_ATTEMPTS` limits the same
document/site pair across relaunches.

## Portal lifecycle

The portal creates one queue item per document/site pair. A worker claims an
item through the worker API, runs fetch in-process, and publishes the result.
Workers receive a scoped credential and do not need direct PostgreSQL access.

PostgreSQL owns queue leases, cancellation fences, reusable job entries, team
access, and the fleet circuit breaker. Standalone fetch runs use SQLite and do
not participate in the portal queue.

## Adding a site

Follow [Adding a site](docs/adding-a-site.md). Capture the wire behavior first,
then implement the browser and fetch versions when the site needs them. Put
site-specific gates and failure modes in `docs/sites/`. Put dated experiments
and measurements in `docs/reports/`.
