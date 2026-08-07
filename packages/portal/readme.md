# Portal

Web interface for managing fetch jobs. Teams upload documents, configure proxy
credentials, track job progress, and download results. Backed by PostgreSQL and
a worker process.

```sh
mise run dev
```

## When to use this

Portal is for teams that want a web UI instead of running fetch commands. It
provides:

- User authentication and team management
- Persistent job history and results
- Proxy credential management (safely stored, not copied to .env)
- Real-time job status via Server-Sent Events
- Result download

## Getting started (local)

Set these in `.env`:

```env
PORTAL_DATABASE_DSN=postgresql://postgres@127.0.0.1:5432/postgres
PORTAL_PUBLIC_ORIGIN=http://localhost:8000
PORTAL_BOOTSTRAP_ADMIN_EMAIL=admin@example.org
PORTAL_BOOTSTRAP_ADMIN_PASSWORD=choose-a-local-password
PORTAL_BOOTSTRAP_TEAM_NAME="Equipo Lima"
PORTAL_BOOTSTRAP_TEAM_SLUG=equipo-lima
PORTAL_SECRET_PROTECTION_KEY=
```

Start the portal:

```sh
mise run dev
```

This starts PostgreSQL in the foreground, applies migrations, provisions the
admin and first team, and runs the app. Ctrl+C stops everything.

To reset local state:

```sh
mise run reset
mise run dev
```

## Provisioning (production)

The portal has no public registration. Create the first admin and team:

```sh
uv run --env-file .env python -m portal.provision \
  --admin-email admin@example.org \
  --admin-password-env PORTAL_PROVISION_ADMIN_PASSWORD \
  --team-name "Equipo Lima" \
  --team-slug equipo-lima
```

Optionally, create proxy credentials from environment variables:

```sh
uv run --env-file .env python -m portal.provision \
  ... \
  --proxy-provider geonode
```

This reads `PORTAL_PROVISION_GEONODE_USERNAME`,
`PORTAL_PROVISION_GEONODE_PASSWORD`, etc., and creates credentials for the team.

The command applies migrations and creates or updates the admin, team,
membership, and proxy credentials. It never prints secret values.

## How it works

### Architecture

```
web              Litestar app: routes, auth, CSRF, SSE
  ├─ routes/
  ├─ deps.py    session/team extraction
  └─ components/ JinjaX templates

application      Team access, job submission, cancellation
  └─ service.py: business logic (teams, credentials, jobs)

domain           Planning and state rules
  ├─ models.py  : Job, Team, Credential types
  └─ planning.py: plan_submission (routes documents to sites)

repository       PostgreSQL modules
  ├─ auth.py
  ├─ teams.py
  ├─ credentials.py
  └─ jobs.py     : job queue and event log

worker           Claims work and publishes results
  └─ main.py    : polls queue, runs fetch, publishes

storage          Immutable object references (file uploads)
  └─ port.py    : abstract interface

security.py      Password, session, CSRF primitives
```

### Job lifecycle

1. **Submit**: User uploads a CSV, selects sites and proxy credentials, clicks
   submit. The portal stores the file, plans the job (routes documents to
   sites), and queues it.

2. **Claim**: The worker polls the queue and claims a queued job with a
   30-minute lease.

3. **Run**: The worker spawns a fetch subprocess with the document CSV and
   configuration. Fetch runs to completion (or Ctrl+C).

4. **Publish**: The worker moves result files from the fetch output to cloud
   storage (configurable), records metadata in the database, and marks the job
   as published.

5. **Recover**: If a worker crashes or the lease expires, another worker claims
   the job (incremented lease fence prevents lost writes).

### Queue and concurrency

Every queue transition locks the singleton `portal_queue_control` row. This
enforces a global limit of five active jobs across all web and worker processes.

Cancellation increments the lease fence before retiring active items. Writes
from older leases are rejected, so you can cancel a job without racing against a
slow worker.

Expired leases are recovered while claiming work. Jobs that repeatedly expire
are marked failed. Jobs that finish without publishing a result are also marked
failed.

Workers use authenticated claim and publish operations. Each claim includes the
proxy configuration for that job.

## Tests

```sh
uv run pytest tests/portal
```

Each test creates, migrates, and drops its own PostgreSQL database. Tests run in
parallel because they share no database state.

## Structure

| Module         | Purpose                                              |
| -------------- | ---------------------------------------------------- |
| `web/`         | Litestar routes, session handling, templates         |
| `application/` | Service layer (teams, credentials, jobs, submission) |
| `domain/`      | Types (Job, Team, Credential) and business rules     |
| `repository/`  | PostgreSQL access (auth, teams, credentials, jobs)   |
| `worker/`      | Background job processor                             |
| `storage/`     | File upload abstraction                              |
| `security.py`  | Password hashing, session tokens, CSRF tokens        |

`security.py` contains password, session, and CSRF primitives shared by the web
and provisioning code. It's also the place to review auth assumptions.

## Configuration

| Variable                       | Meaning                                                           |
| ------------------------------ | ----------------------------------------------------------------- |
| `PORTAL_DATABASE_DSN`          | PostgreSQL connection string                                      |
| `PORTAL_PUBLIC_ORIGIN`         | Scheme and host for CSRF validation (e.g., `https://example.com`) |
| `PORTAL_BOOTSTRAP_*`           | Admin and team created on first run (local dev only)              |
| `PORTAL_SECRET_PROTECTION_KEY` | Encryption key for sensitive values (leave empty in dev)          |

See `portal/application/provisioning.py` for provisioning variables (e.g.,
`PORTAL_PROVISION_GEONODE_USERNAME`).

## Common operations

Table names are prefixed `portal_`; there's no bare `teams`, `jobs`, or `users`.
Membership `role` is `team_leader` or `team_member`.

**Add a user to a team**

```bash
psql postgresql://postgres@127.0.0.1/postgres -c "
  insert into portal_team_memberships (team_id, user_id, role)
  select t.id, u.id, 'team_leader'
  from portal_teams t, portal_users u
  where t.slug = 'equipo-lima'
    and u.email = 'newuser@example.org';
"
```

**List jobs for a team**

```bash
psql postgresql://postgres@127.0.0.1/postgres -c "
  select j.id, j.state, j.created_at
  from portal_jobs j
  join portal_teams t on t.id = j.team_id
  where t.slug = 'equipo-lima'
  order by j.created_at desc
  limit 10;
"
```

**Manually cancel a running job**

Don't set `state = 'cancelled'` directly: that bypasses lease fencing and a
worker holding the old lease can still write results after you think you've
stopped it. Do what `JobsRepository.cancel` does: move to `cancelling` and bump
the fence so writes from the current lease are rejected. The worker retires the
item and moves it to `cancelled` itself.

```bash
psql postgresql://postgres@127.0.0.1/postgres -c "
  update portal_jobs
  set state = 'cancelling',
      lease_fence = lease_fence + 1
  where id = '<job-uuid>'
    and state in ('queued', 'running');
"
```

## See also

- [docs/architecture.md](../../docs/architecture.md): system overview, job
  lifecycle across all packages
- [fetch](../fetch/readme.md): the subprocess the portal spawns for each job
