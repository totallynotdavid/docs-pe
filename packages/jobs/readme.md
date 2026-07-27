# OSIPTEL Jobs

`osiptel-jobs` is an independently deployable internal site for authenticated,
team-scoped batch lookups. It uses the repository's stable `fetch` adapters
(`osiptel`, `sunat`, and `sunat_reps`) and intentionally does not import the
unstable `browser` package.

## Local development

From the repository root, install the workspace once:

```sh
mise run install
cp packages/jobs/.env.example .env.jobs
# Set JOBS_SESSION_SECRET and both JOBS_BOOTSTRAP_ADMIN_* values in .env.jobs.
set -a; source .env.jobs; set +a
uv run osiptel-jobs
```

Open `http://127.0.0.1:8000/login` and sign in with the configured bootstrap
administrator. The administrator can create teams, local users, and team
memberships. A leader submits a single-column UTF-8 CSV. Every physical input
row becomes either an accepted row or a visible exclusion (`invalid_document`,
`duplicate_document`, or `not_supported_by_selected_sources`); no row is
silently discarded.

The first administrator is created only at startup from
`JOBS_BOOTSTRAP_ADMIN_EMAIL` and `JOBS_BOOTSTRAP_ADMIN_PASSWORD`. Both must be
set together. They are never hard-coded in the application or migration.

## Deployment boundary

The control plane is the web service plus its persistent database and immutable
object store. For local development it uses SQLite WAL and a local immutable
directory. Production supplies durable equivalents through the `JOBS_DATABASE_PATH`
and `JOBS_OBJECT_ROOT` seams; migrations run automatically on service startup.
Back up both stores together.

Team proxy settings are entered by a site admin under a GeoNode or DataImpulse
provider. The configuration JSON is encrypted at rest using
`JOBS_SECRET_ENCRYPTION_KEY`; the human API reveals only its provider and secret
reference. It is released only to an authenticated worker holding a current lease
for that team's work. Use a distinct encryption key in production.

Set `JOBS_COOKIE_SECURE=true` behind HTTPS. A deployment cannot enable email or
Kapso WhatsApp delivery without the corresponding `JOBS_EMAIL_DSN` or
`JOBS_KAPSO_API_KEY`; this slice deliberately contains no sender, so configured
external events remain durable outbox work until a delivery deployment is added.

Start one or more outbound workers on any hosts that can reach the service:

```sh
JOBS_ALLOW_LIVE_LOOKUPS=true uv run osiptel-jobs-worker \
  --base-url https://jobs.example.internal \
  --worker-id host-a \
  --capacity 2
```

Workers register outbound, claim short-lived database leases, renew them, and
checkpoint outcomes. No SSH, scp, host-local output database, or remote process
control is part of the product control plane. The service permits at most five
running/cancelling jobs globally; additional jobs stay queued. A cancelled job
stops claims immediately, fences checkpoints from old leases, and keeps prior
server checkpoints while outstanding leases are cancelled or expire.
An item that expires three consecutive leases before a checkpoint becomes
`exhausted` with `worker_lease_expired`; this is a separate worker-recovery cap
and does not consume the source's healthy-contact retry budget.

The worker reuses `fetch_one`, so source warm-up, proxy-session rotation, retry
classification, and local per-provider circuit breaking remain source-owned.
Only idempotent API checkpoints create attempts, terminal results, and progress.
Tests never invoke the worker and the worker refuses live lookups unless
`JOBS_ALLOW_LIVE_LOOKUPS=true` is explicitly set.

## Retention and notifications

Canonical results and immutable exports are retained. Restricted uploaded input
and any future raw diagnostics live in separate object namespaces and are not
normal search results. Search is always team-scoped and membership is checked at
read time, so removing membership revokes access immediately.

Terminal jobs atomically create in-app, email, and Kapso WhatsApp outbox events.
In-app events are available in the service; external email and WhatsApp events
remain disabled unless their explicit deployment flags and delivery credentials
are configured. This slice intentionally includes no external notification sender.
