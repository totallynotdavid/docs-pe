# Portal

Web interface for managing fetch jobs. Teams upload documents, configure proxy
credentials, track job progress, and download results, backed by PostgreSQL and
a worker process. Portal is for teams that want a web UI instead of running
fetch commands: user authentication and team management, persistent job history
and results, proxy credential management (safely stored, not copied to `.env`),
real-time job status via Server-Sent Events, and result download.

```sh
mise run dev
```

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
admin and first team, and runs the app. Ctrl+C stops everything. To reset local
state, run `mise run reset` then `mise run dev` again.

Tests (`uv run pytest tests/portal`) each create, migrate, and drop their own
PostgreSQL database, and run in parallel since they share no database state.

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

## Architecture

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

## How jobs run

1. Submit: the user uploads a CSV, selects sites and proxy credentials, and
   clicks submit. The portal stores the file, plans the job (routes documents to
   sites), and queues it.
2. Claim: the worker polls the queue and claims a queued job with a 30-minute
   lease.
3. Run: the worker spawns a fetch subprocess with the document CSV and
   configuration; fetch runs to completion (or Ctrl+C).
4. Publish: the worker moves result files from the fetch output to cloud storage
   (configurable), records metadata in the database, and marks the job as
   published.
5. Recover: if a worker crashes or the lease expires, another worker claims the
   job (an incremented lease fence prevents lost writes).

Every queue transition locks the singleton `portal_queue_control` row, which
enforces a global limit of five active jobs across all web and worker processes.
Cancellation increments the lease fence before retiring active items, so writes
from older leases are rejected and you can cancel a job without racing against a
slow worker. Expired leases are recovered while claiming work; jobs that
repeatedly expire, or that finish without publishing a result, are marked
failed. Workers use authenticated claim and publish operations, and each claim
includes the proxy configuration for that job. See
[operations.md](operations.md) for the SQL to inspect or manually cancel a job.

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
